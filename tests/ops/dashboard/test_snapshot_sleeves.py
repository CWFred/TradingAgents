"""Sleeves: journal-replay positions, snapshot-based cash/P&L, isolation."""
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from ops import events
from ops.config import OpsConfig
from ops.dashboard.snapshot import build_snapshot
from ops.journal import Journal
from ops.trading_time import trading_day_start

# A fixed mid-week instant (Wednesday 18:00 UTC). Wall-clock now() flaked on
# weekends: NOW-1day must precede trading_day_start(NOW), and a Monday NOW put
# the prior snapshot on a Sunday that could fall the wrong side of the ET
# boundary. A Wednesday keeps NOW-1day (Tue) strictly before the boundary and
# NOW itself strictly after it.
NOW = datetime(2026, 7, 15, 18, 0, tzinfo=timezone.utc)
assert NOW - timedelta(days=1) < trading_day_start(NOW) <= NOW


def _config(tmp_path) -> OpsConfig:
    return OpsConfig(
        journal_path=str(tmp_path / "ops.sqlite"),
        baseline_journal_path=str(tmp_path / "baseline.sqlite"),
        research_journal_path=str(tmp_path / "research.sqlite"),
        screen_store_path=str(tmp_path / "screen.sqlite"),
        memo_store_path=str(tmp_path / "memos.sqlite"),
        guardian_liveness_path=str(tmp_path / "guardian.alive"),
        research_pause_flag_path=str(tmp_path / "research.paused"),
        short_journal_path=str(tmp_path / "short.sqlite"),
        insider_journal_path=str(tmp_path / "insider.sqlite"),
    )


def _seed_momentum(cfg: OpsConfig, now: datetime) -> None:
    # Seed side="BUY" (matches ops.broker.types.Side.BUY.value, which the
    # replay and last_buy_fill_for both key on) and a matching order row so
    # PaperBroker.from_journal replays without hitting its readonly-crashing
    # journal_replay_fallback write path. See task-4 report for the disclosed
    # deviation from the brief's seed (which used "buy" and no order row).
    #
    # The journal carries its cash basis (a seed adjustment + fills), so
    # replay cash (250 seed - 100 BUY fill = 150) is authoritative and
    # intraday-fresh. The last open_day snapshot's cash (160) deliberately
    # diverges so tests can tell which source won.
    with Journal(cfg.journal_path) as j:
        j.record_cash_adjustment(kind="seed", amount=Decimal("250"), note="test")
        j.record_equity_snapshot(
            equity=Decimal("250"), cash=Decimal("250"), kind="open_day",
            at=now - timedelta(days=1))
        j.record_equity_snapshot(
            equity=Decimal("260"), cash=Decimal("160"), kind="open_day", at=now)
        j.record_order(
            client_order_id="c1", symbol="XYZ", side="BUY",
            notional_dollars=Decimal("100"), stop_loss_price=Decimal("9.20"))
        j.record_fill(
            order_id="o1", client_order_id="c1", symbol="XYZ", side="BUY",
            quantity=Decimal("10"), price=Decimal("10"), filled_at=now,
            stop_loss_price=Decimal("9.20"))


def test_sleeve_positions_and_fills_from_replay(tmp_path):
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    assert mom["positions"] == [
        {"symbol": "XYZ", "quantity": "10", "entry": "10", "stop": "9.20"}]
    assert len(mom["fills_today"]) == 1
    assert mom["equity"] == "260"


def test_cash_from_replay_when_journal_has_cash_basis(tmp_path):
    # A seed cash-adjustment means the ledger records its cash basis, so
    # replay cash (250 seed - 100 fill = 150) wins over the stale open_day
    # snapshot's 160 — an intraday BUY must move displayed cash immediately.
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    assert mom["cash"] == "150"


def test_lifetime_pnl_from_first_to_latest_snapshot(tmp_path):
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    # (260 - 250) / 250 = 0.04 over the ledger's lifetime.
    assert Decimal(mom["lifetime_pnl_pct"]) == Decimal("0.04")


def test_lifetime_pnl_null_with_single_snapshot(tmp_path):
    # One snapshot has no baseline to compare against.
    cfg = _config(tmp_path)
    with Journal(cfg.journal_path) as j:
        j.record_cash_adjustment(kind="seed", amount=Decimal("250"), note="t")
        j.record_equity_snapshot(
            equity=Decimal("250"), cash=Decimal("250"), kind="open_day", at=NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    assert mom["lifetime_pnl_pct"] is None


def test_baseline_shaped_cash_is_snapshot_not_negative_replay(tmp_path):
    # Baseline/research services seed cash from config in memory and never
    # journal a seed adjustment. Replaying from 0 would make a single BUY
    # fill drive cash negative; the equity snapshot carries the real cash.
    cfg = _config(tmp_path)
    with Journal(cfg.journal_path) as j:
        j.record_order(
            client_order_id="c1", symbol="XYZ", side="BUY",
            notional_dollars=Decimal("100"), stop_loss_price=Decimal("9.20"))
        j.record_fill(
            order_id="o1", client_order_id="c1", symbol="XYZ", side="BUY",
            quantity=Decimal("10"), price=Decimal("10"), filled_at=NOW,
            stop_loss_price=Decimal("9.20"))
        j.record_equity_snapshot(
            equity=Decimal("10000"), cash=Decimal("9900"), kind="open_day",
            at=NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    assert mom["cash"] == "9900"  # not "-100" from replay-from-0
    assert mom["positions"] == [
        {"symbol": "XYZ", "quantity": "10", "entry": "10", "stop": "9.20"}]


def test_day_pnl_pct_from_consecutive_snapshots(tmp_path):
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    # (260-250)/250 = 0.04
    assert Decimal(mom["day_pnl_pct"]) == Decimal("0.04")


def test_day_pnl_null_when_no_snapshot_today(tmp_path):
    # Only a prior-day snapshot exists; nothing for today yet (pre-open /
    # weekend). day_pnl must be null, not a false 0.00%.
    cfg = _config(tmp_path)
    with Journal(cfg.journal_path) as j:
        j.record_cash_adjustment(kind="seed", amount=Decimal("250"), note="t")
        j.record_equity_snapshot(
            equity=Decimal("250"), cash=Decimal("250"), kind="open_day",
            at=NOW - timedelta(days=1))
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    assert mom["day_pnl_pct"] is None
    assert mom["equity"] == "250"  # latest snapshot still reported


def test_missing_sleeve_journal_isolated(tmp_path):
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)  # research + baseline journals never created
    sleeves = build_snapshot(cfg, now=NOW)["sleeves"]
    assert "error" in sleeves["research"]
    assert "error" in sleeves["baseline"]
    assert "error" not in sleeves["momentum"]


def test_anomalies_counts_and_last_at(tmp_path):
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)
    with Journal(cfg.journal_path) as j:
        j.record_event(events.KIND_STOP_FAILED, {"symbol": "XYZ"})
        j.record_event(events.KIND_STOP_FAILED, {"symbol": "XYZ"})
    anom = build_snapshot(cfg, now=NOW)["anomalies_7d"]
    assert anom[events.KIND_STOP_FAILED]["count"] == 2
    assert anom[events.KIND_STOP_FAILED]["last_at"] is not None
    assert anom[events.KIND_GUARDIAN_BLIND]["count"] == 0


def test_short_sleeve_positions_replay_through_short_broker(tmp_path):
    cfg = _config(tmp_path)
    with Journal(cfg.short_journal_path) as j:
        j.record_equity_snapshot(
            equity=Decimal("10020"), cash=Decimal("10400"), kind="short_run", at=NOW)
        j.record_order(
            client_order_id="s1", symbol="GHST", side="SHORT",
            notional_dollars=Decimal("400"), stop_loss_price=None)
        j.record_fill(
            order_id="o1", client_order_id="s1", symbol="GHST", side="SHORT",
            quantity=Decimal("40"), price=Decimal("10"), filled_at=NOW)
    short = build_snapshot(cfg, now=NOW)["sleeves"]["short"]
    # PaperBroker replay would skip the SHORT fill and show no positions;
    # the ShortPaperBroker dispatch is what makes this visible.
    assert short["positions"] == [
        {"symbol": "GHST", "quantity": "40", "entry": "10", "stop": None}]
    assert short["equity"] == "10020"
    assert short["collateral_cash"] == "10400"
    assert short["gross_short_exposure"] == "380"
    assert Decimal(short["gross_short_exposure_pct"]) == Decimal("380") / Decimal("10020")
    assert short["unrealized_pnl"] == "20"
    assert len(short["fills_today"]) == 1


def test_long_sleeve_short_metrics_are_null(tmp_path):
    cfg = _config(tmp_path)
    _seed_momentum(cfg, NOW)
    mom = build_snapshot(cfg, now=NOW)["sleeves"]["momentum"]
    assert mom["collateral_cash"] is None
    assert mom["gross_short_exposure"] is None
    assert mom["gross_short_exposure_pct"] is None
    assert mom["unrealized_pnl"] is None


def test_insider_sleeve_uses_long_replay(tmp_path):
    cfg = _config(tmp_path)
    with Journal(cfg.insider_journal_path) as j:
        j.record_equity_snapshot(
            equity=Decimal("10300"), cash=Decimal("9700"), kind="insider_run", at=NOW)
        j.record_order(
            client_order_id="i1", symbol="AAA", side="BUY",
            notional_dollars=Decimal("300"), stop_loss_price=None)
        j.record_fill(
            order_id="o1", client_order_id="i1", symbol="AAA", side="BUY",
            quantity=Decimal("30"), price=Decimal("10"), filled_at=NOW)
    insider = build_snapshot(cfg, now=NOW)["sleeves"]["insider"]
    assert insider["positions"] == [
        {"symbol": "AAA", "quantity": "30", "entry": "10", "stop": None}]
    assert insider["equity"] == "10300"


def test_missing_new_sleeve_journals_are_per_sleeve_errors(tmp_path):
    cfg = _config(tmp_path)  # neither file exists
    sleeves = build_snapshot(cfg, now=NOW)["sleeves"]
    assert "error" in sleeves["short"]
    assert "error" in sleeves["insider"]


def test_frontend_sleeve_order_covers_every_backend_sleeve(tmp_path):
    # Guard source↔backend drift only: the frontend TypeScript source declares
    # SLEEVE_ORDER and the backend emits sleeves. If they diverge, the frontend
    # list may omit a backend sleeve (and it disappears silently from the UI).
    # This test ensures the source SLEEVE_ORDER stays in sync with backend sleeves.
    # (Does NOT guard against stale shipped bundle — see
    # test_shipped_bundle_contains_every_backend_sleeve for that.)
    import re
    from pathlib import Path

    # Read from source TypeScript (not the minified built output)
    types_ts = (Path(__file__).parent.parent.parent.parent / "dashboard-ui" / "src" / "data" / "types.ts").read_text()
    m = re.search(r"export const SLEEVE_ORDER = \[(.*?)\]", types_ts)
    assert m, "SLEEVE_ORDER constant not found in types.ts"
    frontend = set(re.findall(r'"(\w+)"', m.group(1)))

    backend = set(build_snapshot(_config(tmp_path), now=NOW)["sleeves"])
    assert frontend == backend


def test_shipped_bundle_contains_every_backend_sleeve(tmp_path):
    # Guard against stale shipped bundle: the built React app (ops/dashboard/
    # static/assets/app.js) is committed to the repo but may fall out of sync if
    # someone edits TypeScript (e.g., adds a sleeve to SLEEVE_ORDER) and backend
    # without running `npm run build`. Sleeve names survive minification as quoted
    # string literals, so we can grep for them directly in the bundle.
    from pathlib import Path

    # Derive the backend sleeve set (same as the existing test does)
    backend = set(build_snapshot(_config(tmp_path), now=NOW)["sleeves"])

    # Read the shipped minified bundle
    bundle_path = (Path(__file__).parent.parent.parent.parent / "ops" / "dashboard" / "static" / "assets" / "app.js")
    bundle_bytes = bundle_path.read_bytes()

    # Quoted-substring check is per-name best-effort: generic words like "short" may appear in non-sleeve literals; a NEW sleeve name missing from a stale bundle is always caught.
    for sleeve in backend:
        quoted_sleeve = f'"{sleeve}"'
        assert quoted_sleeve.encode() in bundle_bytes, \
            f'Sleeve "{sleeve}" not found in shipped bundle at {bundle_path}. Run: npm run build'

    # Sanity check: assert a name NOT in the bundle is correctly absent
    assert b'"notasleeve"' not in bundle_bytes


def test_shipped_bundle_contains_activity_ui():
    """The built app must ship the activity strip and runs panel — a stale
    bundle silently hides the feature."""
    from pathlib import Path

    bundle_path = (Path(__file__).parent.parent.parent.parent / "ops" / "dashboard" / "static" / "assets" / "app.js")
    bundle_bytes = bundle_path.read_bytes()
    for needle in (b"now-strip", b"no runs recorded yet", b"activity"):
        assert needle in bundle_bytes, f"missing {needle!r} in shipped bundle"
