"""Startup state reconciliation between journal replay and live broker."""
from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

from ops import events
from ops.journal import Journal


_EPSILON_QTY = Decimal("1e-6")
_EPSILON_CASH = Decimal("0.01")


@dataclass(frozen=True)
class PositionDiff:
    symbol: str
    journal_qty: Decimal | None
    broker_qty: Decimal | None
    kind: str   # "extra_in_broker" | "extra_in_journal" | "qty_mismatch" | "cash_drift"


@dataclass(frozen=True)
class ReconcileResult:
    diffs: list[PositionDiff]
    cash_journal: Decimal
    cash_broker: Decimal
    cash_diff: Decimal
    positions_recovered_without_stops: list[str] = field(default_factory=list)


def reconcile(*, journal: Journal, broker: Any, broker_mode: str) -> ReconcileResult:
    """Compare journal-replayed state to live broker state.

    Paper: journal is authoritative; the guarded PaperBroker was built
    from the same journal, so the two should agree exactly. Any diff
    indicates a bug we want to catch.

    Live (robinhood): compare per-symbol qty and cash; diffs are
    surfaced as PositionDiff objects for the caller to journal + halt on.
    """
    from ops.broker.paper import PaperBroker

    replay = PaperBroker.from_journal(
        journal=journal,
        quote_source=broker.get_quote,
        starting_cash=Decimal("0"),
    )
    replay_positions = {p.symbol: p.quantity for p in replay.get_positions()}
    broker_positions = {p.symbol: p.quantity for p in broker.get_positions()}

    diffs: list[PositionDiff] = []
    all_symbols = set(replay_positions) | set(broker_positions)
    for symbol in sorted(all_symbols):
        jq = replay_positions.get(symbol)
        bq = broker_positions.get(symbol)
        if jq is None:
            diffs.append(PositionDiff(symbol=symbol, journal_qty=None, broker_qty=bq, kind="extra_in_broker"))
        elif bq is None:
            diffs.append(PositionDiff(symbol=symbol, journal_qty=jq, broker_qty=None, kind="extra_in_journal"))
        elif abs(jq - bq) > _EPSILON_QTY:
            diffs.append(PositionDiff(symbol=symbol, journal_qty=jq, broker_qty=bq, kind="qty_mismatch"))

    cash_journal = replay.get_cash()
    cash_broker = broker.get_cash()
    cash_diff = cash_broker - cash_journal

    # Live mode: any material cash drift halts startup. Paper mode carries a
    # known structural offset (live PaperBroker's starting_cash vs replay's
    # starting_cash=0), so cash drift is not a diff there.
    if broker_mode == "robinhood" and abs(cash_diff) > _EPSILON_CASH:
        diffs.append(PositionDiff(
            symbol="__CASH__",
            journal_qty=cash_journal, broker_qty=cash_broker,
            kind="cash_drift",
        ))

    positions_recovered_without_stops = sorted(
        p.symbol for p in broker.get_positions() if p.stop_loss_price is None
    )

    return ReconcileResult(
        diffs=diffs,
        cash_journal=cash_journal, cash_broker=cash_broker,
        cash_diff=cash_diff,
        positions_recovered_without_stops=positions_recovered_without_stops,
    )


def emit_reconcile_events(journal: Journal, result: ReconcileResult) -> None:
    if result.diffs:
        journal.record_event(
            events.KIND_INCONSISTENCY,
            events.inconsistency_payload(
                diffs=[
                    {
                        "symbol": d.symbol,
                        "journal_qty": str(d.journal_qty) if d.journal_qty is not None else None,
                        "broker_qty": str(d.broker_qty) if d.broker_qty is not None else None,
                        "kind": d.kind,
                    }
                    for d in result.diffs
                ],
                cash_journal=result.cash_journal,
                cash_broker=result.cash_broker,
                cash_diff=result.cash_diff,
            ),
        )
    if result.positions_recovered_without_stops:
        journal.record_event(
            events.KIND_POSITIONS_RECOVERED_WITHOUT_STOPS,
            events.positions_recovered_without_stops_payload(
                symbols=result.positions_recovered_without_stops,
            ),
        )
