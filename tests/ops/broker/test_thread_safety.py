"""Regression coverage for L1 (Journal/PaperBroker cross-thread access).

PaperBroker keeps its positions in a plain dict mutated in place by
place_order/close_position. GuardedBroker.get_positions/get_equity/get_cash
used to read that dict with no lock at all, while place_order/close_position
mutated it (adding/removing keys -- changing dict size) under
GuardedBroker._lock, a lock the readers never acquired. A guardian thread
spinning on get_positions() while the orchestrator thread opens/closes
positions could observe the dict mid-mutation and blow up with
"dictionary changed size during iteration".

These tests spin several writer threads (each opening and closing a
distinct symbol) against several reader threads (spinning on the read
methods) for a short, bounded number of iterations. `sys.setswitchinterval`
is lowered for the duration of the test only, to make the GIL hand off
control frequently enough that the race reproduces reliably without needing
a slow, high-iteration-count loop.
"""
from __future__ import annotations

import sys
import threading
from decimal import Decimal

from ops.broker.guarded import GuardedBroker
from ops.broker.paper import PaperBroker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.guardrails.engine import RuleEngine
from ops.journal import Journal

_N_WRITER_THREADS = 6
_N_READER_THREADS = 6
_SYMBOLS_PER_WRITER = 40


def _run_concurrent_read_write(tmp_path) -> list[BaseException]:
    journal = Journal(str(tmp_path / "j.sqlite"))
    prices: dict[str, Decimal] = {}

    def quote_source(symbol: str) -> Decimal:
        return prices.setdefault(symbol, Decimal("10"))

    inner = PaperBroker(
        journal=journal, quote_source=quote_source, starting_cash=Decimal("10000000"),
    )
    guarded = GuardedBroker(
        inner=inner, engine=RuleEngine([]), journal=journal, config=OpsConfig(),
    )

    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(worker_idx: int) -> None:
        for i in range(_SYMBOLS_PER_WRITER):
            symbol = f"SYM{worker_idx}-{i}"
            try:
                guarded.place_order(Order(
                    client_order_id=f"o-{worker_idx}-{i}", symbol=symbol, side=Side.BUY,
                    notional_dollars=Decimal("10"), order_type=OrderType.MARKET,
                    stop_pct=Decimal("-0.1"),
                ))
                guarded.close_position(symbol)
            except BaseException as exc:  # noqa: BLE001 - captured for the assertion
                errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            try:
                guarded.get_positions()
                guarded.get_equity()
                guarded.get_cash()
            except BaseException as exc:  # noqa: BLE001 - captured for the assertion
                errors.append(exc)

    old_interval = sys.getswitchinterval()
    sys.setswitchinterval(1e-6)
    try:
        readers = [threading.Thread(target=reader) for _ in range(_N_READER_THREADS)]
        writers = [
            threading.Thread(target=writer, args=(idx,)) for idx in range(_N_WRITER_THREADS)
        ]
        for t in readers + writers:
            t.start()
        for t in writers:
            t.join()
        stop.set()
        for t in readers:
            t.join()
    finally:
        sys.setswitchinterval(old_interval)

    return errors


def test_guarded_positions_survive_concurrent_reads_and_writes(tmp_path):
    errors = _run_concurrent_read_write(tmp_path)
    assert errors == [], [repr(e) for e in errors]


def test_journal_lock_serializes_reads_against_writes(tmp_path):
    """Deterministic proof that Journal._lock actually serializes callers.

    Forcing the underlying unsynchronized-sqlite3 race directly is
    non-deterministic, and empirically dangerous to boot: an earlier version
    of this test drove many threads at sqlite3's C-level connection/cursor
    objects with an aggressively lowered `sys.setswitchinterval` to try to
    force a crash, and instead of failing fast it wedged the interpreter for
    tens of minutes of CPU time under heavy thread contention -- not
    something a test suite should risk. So instead of trying to force the
    race, this test verifies the mechanism the fix provides directly: one
    thread holds `journal._lock` (simulating a write in progress); a
    concurrent `read_events()` call on another thread must block on that
    same lock rather than run, and only completes after the holder releases
    it. Pre-fix, `Journal` has no `_lock` attribute at all, so this fails
    with AttributeError -- the exact missing piece L1 requires.
    """
    journal = Journal(str(tmp_path / "j.sqlite"))
    journal.record_event("seed", {})  # give read_events() something to fetch

    lock_acquired = threading.Event()
    release_lock = threading.Event()
    read_done = threading.Event()

    def hold_lock():
        with journal._lock:
            lock_acquired.set()
            release_lock.wait(timeout=5)

    def do_read():
        journal.read_events()
        read_done.set()

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert lock_acquired.wait(timeout=5), "writer never acquired journal._lock"

    reader = threading.Thread(target=do_read)
    reader.start()
    # The reader must be blocked on the same lock -- give it ample time to
    # prove it does NOT complete while the holder is still inside the lock.
    assert not read_done.wait(timeout=0.2), (
        "read_events() ran while another thread held journal._lock -- "
        "reads are not serialized against writes"
    )

    release_lock.set()
    holder.join(timeout=5)
    assert read_done.wait(timeout=5), "read_events() never completed after lock release"
    reader.join(timeout=5)


def test_journal_survives_concurrent_readers_and_writers(tmp_path):
    """Direct Journal-level stress at a realistic, non-adversarial thread
    count: several threads writing events/orders while others read them
    back concurrently, for a short bounded number of iterations. Guards
    against non-atomic read-modify-use sequences now that every Journal
    method serializes through one lock and materializes cursor rows to a
    list before releasing it."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    errors: list[BaseException] = []
    stop = threading.Event()

    def writer(worker_idx: int) -> None:
        for i in range(_SYMBOLS_PER_WRITER):
            try:
                journal.record_event("test_event", {"worker": worker_idx, "i": i})
                journal.record_order(
                    client_order_id=f"j-{worker_idx}-{i}", symbol="AAPL", side="BUY",
                    notional_dollars=Decimal("10"), stop_loss_price=None,
                )
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    def reader() -> None:
        while not stop.is_set():
            try:
                journal.read_events()
                journal.read_orders()
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

    readers = [threading.Thread(target=reader) for _ in range(_N_READER_THREADS)]
    writers = [
        threading.Thread(target=writer, args=(idx,)) for idx in range(_N_WRITER_THREADS)
    ]
    for t in readers + writers:
        t.start()
    for t in writers:
        t.join(timeout=30)
    stop.set()
    for t in readers:
        t.join(timeout=30)

    assert errors == [], [repr(e) for e in errors]
