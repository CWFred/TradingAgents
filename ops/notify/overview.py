"""Daily cross-sleeve overview: "everything that happened today" (DO-Task 2).

build_daily_overview reads ONLY the three journals (main/momentum, research,
baseline) plus the memo store — no broker, no MCP, no quotes, no LLM, no
network — so it is always safe to run on a schedule regardless of broker
reachability. This mirrors the journal-only discipline of ops/status.py
(single-sleeve system snapshot) and ops/research/report.py (calibration
report); this module is the union across all three sleeves for a single
trading day.

format_daily_overview is a pure renderer over the dict (markdown, `#`/`##`
headers) — the build/format split mirrors both precedents above.
overview_headline reduces the same dict to a single push-notification line.

Every section degrades gracefully to "none"/"n/a" when its source has no
matching events, and the whole report degrades to a "Quiet day" banner when
nothing happened anywhere — day one (empty journals, empty memo store) must
render without raising, because the daemon runs this on a schedule from the
very first weekday.

Money stays Decimal inside the dict; stringification happens only in
format_daily_overview / overview_headline (see ops/status.py's docstring for
the same convention).

Design note on momentum "day equity + P&L": the momentum sleeve journals
exactly one equity snapshot per trading day (`open_day`, taken by the
orchestrator before that day's cycle runs) and nothing else — there is no
journaled "current"/"closing" equity point, and fetching one would require a
broker or a quote, both forbidden here. So "P&L" is computed as the change
between *today's* open_day snapshot and the most recent open_day snapshot
strictly before today (i.e. day-over-day change in opening equity) rather
than an intraday mark-to-market P&L, which this module structurally cannot
know. It is None when there is no prior open_day snapshot to compare against
(including day one).
"""
from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal
from typing import Any

from ops import events
from ops.trading_time import trading_day_start

# Anomaly kinds surfaced by section 4, drawn from the main/research/baseline
# journals. Resolves the plan's "*_error"/"research_*_error" wildcards to
# the concrete kinds registered in ops/events.py today.
_MAIN_ANOMALY_KINDS = (
    events.KIND_DAILY_HALT,
    events.KIND_KILL_SWITCH,
    events.KIND_STOP_HIT,
    events.KIND_STOP_FAILED,
    events.KIND_ORDER_REJECTED,
    events.KIND_UNIVERSE_BLIND,
    events.KIND_GUARDIAN_CHECK_ERROR,
    events.KIND_GUARDIAN_BLIND,
    events.KIND_ORCHESTRATOR_TICK_ERROR,
    events.KIND_EXIT_CHECK_ERROR,
    events.KIND_HEARTBEAT_ERROR,
    events.KIND_BROKER_UNREACHABLE,
    # research_monitor_error/research_trade_error are recorded on the MAIN
    # journal, not the research journal -- both _research_monitor_tick and
    # _research_trade_tick (ops/main.py) record onto the daemon's `journal`
    # param, which is the main journal, same as research_monitor_run and
    # research_trade_run.
    events.KIND_RESEARCH_MONITOR_ERROR,
    events.KIND_RESEARCH_TRADE_ERROR,
    # Short-sleeve errors land on the MAIN journal too (_short_trade_tick /
    # _short_overnight_pass record onto the daemon's journal param).
    events.KIND_SHORT_TRADE_ERROR,
    events.KIND_SHORT_DRAIN_ERROR,
    events.KIND_SHORT_VETTING_ERROR,
)
# No research- or baseline-journal kind matches the plan's
# "*_error"/explicit-name list today (baseline_quote_failure is a near-miss
# but not literally an "_error" kind or one of the named anomalies) -- kept
# empty rather than widening the spec on our own judgment call.
_RESEARCH_ANOMALY_KINDS: tuple[str, ...] = ()
_BASELINE_ANOMALY_KINDS: tuple[str, ...] = ()


def _day_slice(journal: Any, day_start: datetime) -> dict[str, list[dict[str, Any]]]:
    """Today's events from `journal`, grouped by kind.

    A single read_events() pass plus an `at >= day_start` filter, reused by
    every section below — cheaper than one journal query per section, and it
    is the one place that defines "today" for event-sourced sections (memo
    filtering does its own created_at comparison since memos are not
    journal events)."""
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for ev in journal.read_events():
        if ev["at"] >= day_start:
            by_kind.setdefault(ev["kind"], []).append(ev)
    return by_kind


def _momentum_equity_and_pnl(
    main_journal: Any, day_start: datetime,
) -> tuple[Decimal | None, datetime | None, Decimal | None]:
    """Today's open_day equity + day-over-day pct change vs the prior
    open_day snapshot. See module docstring for why this is the only
    journal-only definition of "P&L" available here."""
    snaps = sorted(
        (s for s in main_journal.read_equity_snapshots() if s["kind"] == "open_day"),
        key=lambda s: s["at"],
    )
    today = [s for s in snaps if s["at"] >= day_start]
    if not today:
        return None, None, None
    latest = today[-1]
    prior = [s for s in snaps if s["at"] < day_start]
    pnl_pct: Decimal | None = None
    if prior and prior[-1]["equity"] != 0:
        prev_equity = prior[-1]["equity"]
        pnl_pct = (latest["equity"] - prev_equity) / prev_equity
    return latest["equity"], latest["at"], pnl_pct


def _momentum_section(
    main_journal: Any, by_kind: dict[str, list[dict[str, Any]]], day_start: datetime,
) -> dict[str, Any]:
    cycle_ran = bool(by_kind.get(events.KIND_DAILY_CYCLE_RUN))

    diag_events = by_kind.get(events.KIND_UNIVERSE_DIAGNOSTICS, [])
    universe: dict[str, int] | None = None
    if diag_events:
        diag = diag_events[-1]["payload"]
        universe = {
            "checked": diag["fetch_ok"] + diag["fetch_failed"],
            "fetch_failures": diag["fetch_failed"],
            "candidates": diag["candidates"],
        }
    universe_blind = bool(by_kind.get(events.KIND_UNIVERSE_BLIND))

    by_verdict: dict[str, list[str]] = {"BUY": [], "HOLD": [], "SELL": []}
    for ev in by_kind.get(events.KIND_ANALYSIS_DECISION, []):
        payload = ev["payload"]
        by_verdict.setdefault(payload["decision"], []).append(payload["symbol"])
    analyzed_decided = {
        "total": sum(len(v) for v in by_verdict.values()),
        "by_verdict": by_verdict,
    }

    buys_filled = [
        ev["payload"]["symbol"] for ev in by_kind.get(events.KIND_FILL, [])
        if ev["payload"]["side"] == "BUY"
    ]

    rejected = [
        {"symbol": ev["payload"]["symbol"], "reason": ev["payload"]["reason"]}
        for ev in by_kind.get(events.KIND_ORDER_REJECTED, [])
    ]

    exits = [
        {"symbol": ev["payload"]["symbol"], "rule": ev["payload"]["rule"]}
        for ev in by_kind.get(events.KIND_EXIT_DECISION, [])
    ]

    day_equity, day_equity_at, day_pnl_pct = _momentum_equity_and_pnl(main_journal, day_start)

    return {
        "cycle_ran": cycle_ran,
        "universe": universe,
        "universe_blind": universe_blind,
        "analyzed_decided": analyzed_decided,
        "buys_filled": buys_filled,
        "rejected": rejected,
        "exits": exits,
        "day_equity": day_equity,
        "day_equity_at": day_equity_at,
        "day_pnl_pct": day_pnl_pct,
    }


def _research_section(
    main_by_kind: dict[str, list[dict[str, Any]]],
    research_by_kind: dict[str, list[dict[str, Any]]],
    memos_today: list[Any],
) -> dict[str, Any]:
    # Buy/pass recommendation is not a stored field (per the plan) — status
    # (open/passed/resolved) is the closest proxy available at write time.
    memos = [
        {
            "ticker": m.ticker,
            "thesis_type": m.thesis_type,
            "tier": m.conviction_tier,
            "status": m.status,
        }
        for m in memos_today
    ]

    # research_monitor_run/falsifier_tripped/research_escalation/
    # resolution_due/catalyst_due (Phase C, ops/research/monitor.py) and
    # research_trade_run (Phase D, ops/research/trading.py:272) are all
    # recorded on the MAIN journal, not the research journal — only
    # research_position_opened/closed (and the research_run equity snapshot,
    # read separately in the header) live in the research journal.
    monitor_runs = main_by_kind.get(events.KIND_RESEARCH_MONITOR_RUN, [])
    monitor_counts: dict[str, int] | None = None
    if monitor_runs:
        p = monitor_runs[-1]["payload"]
        monitor_counts = {
            "memos_checked": p["memos_checked"],
            "falsifiers_evaluated": p["falsifiers_evaluated"],
            "tripped": p["tripped"],
            "unevaluable": p["unevaluable"],
            "escalations": p["escalations"],
            "resolution_due": p["resolution_due"],
            "catalyst_due": p["catalyst_due"],
        }

    tripped = [
        ev["payload"]["ticker"] for ev in main_by_kind.get(events.KIND_FALSIFIER_TRIPPED, [])
    ]
    escalations = [
        ev["payload"]["ticker"] for ev in main_by_kind.get(events.KIND_RESEARCH_ESCALATION, [])
    ]
    resolution_due = [
        ev["payload"]["ticker"] for ev in main_by_kind.get(events.KIND_RESOLUTION_DUE, [])
    ]
    catalyst_due = [
        ev["payload"]["ticker"] for ev in main_by_kind.get(events.KIND_CATALYST_DUE, [])
    ]

    trade_runs = main_by_kind.get(events.KIND_RESEARCH_TRADE_RUN, [])
    trades: dict[str, Any] | None = None
    if trade_runs:
        tp = trade_runs[-1]["payload"]
        trades = {
            "entered": tp["entered"],
            "exited": tp["exited"],
            "skipped": tp["skipped"],
            "equity": Decimal(tp["equity"]),
            "cash": Decimal(tp["cash"]),
        }

    positions_opened = [
        {
            "symbol": ev["payload"]["symbol"],
            "memo_id": ev["payload"]["memo_id"],
            "tier": ev["payload"]["conviction_tier"],
        }
        for ev in research_by_kind.get(events.KIND_RESEARCH_POSITION_OPENED, [])
    ]
    positions_closed = [
        {
            "symbol": ev["payload"]["symbol"],
            "memo_id": ev["payload"]["memo_id"],
            "reason": ev["payload"]["reason"],
        }
        for ev in research_by_kind.get(events.KIND_RESEARCH_POSITION_CLOSED, [])
    ]

    return {
        "memos": memos,
        "monitor": {
            "counts": monitor_counts,
            "tripped": tripped,
            "escalations": escalations,
            "resolution_due": resolution_due,
            "catalyst_due": catalyst_due,
        },
        "trades": trades,
        "positions_opened": positions_opened,
        "positions_closed": positions_closed,
    }


def _short_section(
    main_by_kind: dict[str, list[dict[str, Any]]],
    short_by_kind: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, Any]:
    """Short-sleeve slice of the day. ``short_by_kind`` is None when the
    sleeve's journal isn't configured/present — the section then renders a
    not-configured line instead of pretending the sleeve is quiet.
    short_trade_run and the overnight drain/vetting summaries live on the
    MAIN journal (same discipline as the research kinds); only
    short_position_opened/closed live in the short journal."""
    if short_by_kind is None:
        return {"configured": False, "trades": None, "overnight": None,
                "positions_opened": [], "positions_closed": []}

    trade_runs = main_by_kind.get(events.KIND_SHORT_TRADE_RUN, [])
    trades: dict[str, Any] | None = None
    if trade_runs:
        tp = trade_runs[-1]["payload"]
        trades = {
            "entered": tp["entered"],
            "exited": tp["exited"],
            "skipped": tp["skipped"],
            "equity": Decimal(tp["equity"]),
            "cash": Decimal(tp["cash"]),
        }

    overnight: dict[str, Any] | None = None
    drain_runs = main_by_kind.get(events.KIND_SHORT_DRAIN_RUN, [])
    vet_runs = main_by_kind.get(events.KIND_SHORT_VETTING_RUN, [])
    if drain_runs or vet_runs:
        dp = drain_runs[-1]["payload"] if drain_runs else {}
        vp = vet_runs[-1]["payload"] if vet_runs else {}
        overnight = {
            "screened": dp.get("screened_this_run", False),
            "researched": dp.get("researched", 0),
            "still_pending": dp.get("still_pending", 0),
            "vetted": vp.get("vetted", 0),
            "confirmed": vp.get("confirmed", 0),
            "rejected": vp.get("rejected", 0),
        }

    positions_opened = [
        {
            "symbol": ev["payload"]["symbol"],
            "memo_id": ev["payload"]["memo_id"],
            "tier": ev["payload"]["conviction_tier"],
        }
        for ev in short_by_kind.get(events.KIND_SHORT_POSITION_OPENED, [])
    ]
    positions_closed = [
        {
            "symbol": ev["payload"]["symbol"],
            "memo_id": ev["payload"]["memo_id"],
            "reason": ev["payload"]["reason"],
        }
        for ev in short_by_kind.get(events.KIND_SHORT_POSITION_CLOSED, [])
    ]

    return {
        "configured": True,
        "trades": trades,
        "overnight": overnight,
        "positions_opened": positions_opened,
        "positions_closed": positions_closed,
    }


def _baseline_section(by_kind: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    screen_runs = by_kind.get(events.KIND_BASELINE_SCREEN_RUN, [])
    screen: dict[str, Any] | None = None
    if screen_runs:
        p = screen_runs[-1]["payload"]
        screen = {
            "passers": p["passers"],
            "buys": p["buys"],
            "exits": p["exits"],
            "skipped": p["skipped"],
            "equity": Decimal(p["equity"]),
        }

    exits = [
        {"symbol": ev["payload"]["symbol"], "held_days": ev["payload"]["held_days"]}
        for ev in by_kind.get(events.KIND_BASELINE_EXIT, [])
    ]

    writeoffs = [
        {"symbol": ev["payload"]["symbol"], "kind": "auto"}
        for ev in by_kind.get(events.KIND_BASELINE_AUTO_WRITEOFF, [])
    ] + [
        {"symbol": ev["payload"]["symbol"], "kind": "manual"}
        for ev in by_kind.get(events.KIND_BASELINE_WRITEOFF, [])
    ]

    return {"screen": screen, "exits": exits, "writeoffs": writeoffs}


def _anomalies_section(
    main_by_kind: dict[str, list[dict[str, Any]]],
    research_by_kind: dict[str, list[dict[str, Any]]],
    baseline_by_kind: dict[str, list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    anomalies: list[dict[str, Any]] = []
    for by_kind, kinds in (
        (main_by_kind, _MAIN_ANOMALY_KINDS),
        (research_by_kind, _RESEARCH_ANOMALY_KINDS),
        (baseline_by_kind, _BASELINE_ANOMALY_KINDS),
    ):
        for kind in kinds:
            for ev in by_kind.get(kind, []):
                anomalies.append({"kind": kind, "at": ev["at"], "payload": ev["payload"]})
    anomalies.sort(key=lambda a: a["at"])
    return anomalies


def _sleeve_snapshot(journal: Any, kind: str) -> dict[str, Any] | None:
    snap = journal.get_latest_equity_snapshot(kind=kind)
    if snap is None:
        return None
    return {"equity": snap.equity, "at": snap.at}


def _header_section(
    when: datetime, main_journal: Any, research_journal: Any, baseline_journal: Any,
    *, short_journal: Any | None = None,
) -> dict[str, Any]:
    return {
        "date": when.date(),
        "momentum": _sleeve_snapshot(main_journal, "open_day"),
        "research": _sleeve_snapshot(research_journal, "research_run"),
        "baseline": _sleeve_snapshot(baseline_journal, "baseline_run"),
        "short": (_sleeve_snapshot(short_journal, "short_run")
                  if short_journal is not None else None),
    }


def _is_quiet(
    momentum: dict[str, Any], research: dict[str, Any], baseline: dict[str, Any],
    anomalies: list[dict[str, Any]], *, short: dict[str, Any] | None = None,
) -> bool:
    monitor = research["monitor"]
    short_active = short is not None and short["configured"] and (
        short["trades"] is not None
        or short["positions_opened"]
        or short["positions_closed"]
        or (short["overnight"] is not None
            and (short["overnight"]["researched"] or short["overnight"]["vetted"]))
    )
    if short_active:
        return False
    return (
        not momentum["cycle_ran"]
        and momentum["universe"] is None
        and momentum["analyzed_decided"]["total"] == 0
        and not momentum["buys_filled"]
        and not momentum["rejected"]
        and not momentum["exits"]
        and not research["memos"]
        and monitor["counts"] is None
        and not monitor["tripped"]
        and not monitor["escalations"]
        and not monitor["resolution_due"]
        and not monitor["catalyst_due"]
        and research["trades"] is None
        and not research["positions_opened"]
        and not research["positions_closed"]
        and baseline["screen"] is None
        and not baseline["exits"]
        and not baseline["writeoffs"]
        and not anomalies
    )


def build_daily_overview(
    *, main_journal: Any, baseline_journal: Any, research_journal: Any, memo_store: Any,
    config: Any, now: datetime | None = None, short_journal: Any | None = None,
) -> dict[str, Any]:
    """Assemble the daily overview dict from the three journals + memo store.

    Pure aggregation over what was already journaled/memoed today — no
    network, no broker, no quotes, no LLM (see module docstring). `config`
    is accepted for interface parity with the other build_* functions and
    forward compatibility; no config field is consulted today.
    """
    when = now if now is not None else datetime.now(timezone.utc)
    day_start = trading_day_start(when)

    main_by_kind = _day_slice(main_journal, day_start)
    research_by_kind = _day_slice(research_journal, day_start)
    baseline_by_kind = _day_slice(baseline_journal, day_start)
    short_by_kind = _day_slice(short_journal, day_start) if short_journal is not None else None

    memos_today = [m for m in memo_store.list() if m.created_at >= day_start]

    momentum = _momentum_section(main_journal, main_by_kind, day_start)
    research = _research_section(main_by_kind, research_by_kind, memos_today)
    short = _short_section(main_by_kind, short_by_kind)
    baseline = _baseline_section(baseline_by_kind)
    anomalies = _anomalies_section(main_by_kind, research_by_kind, baseline_by_kind)
    header = _header_section(when, main_journal, research_journal, baseline_journal,
                             short_journal=short_journal)

    return {
        "date": when.date(),
        "generated_at": when,
        "quiet": _is_quiet(momentum, research, baseline, anomalies, short=short),
        "header": header,
        "momentum": momentum,
        "research": research,
        "short": short,
        "baseline": baseline,
        "anomalies": anomalies,
    }


def _fmt_money(value: Decimal | None) -> str:
    return f"${value:,.2f}" if value is not None else "n/a"


def _fmt_dt(value: datetime | None) -> str:
    return value.isoformat() if value is not None else "n/a"


def _fmt_pct(value: Decimal | None) -> str | None:
    return f"{value:+.2%}" if value is not None else None


def _format_header(header: dict[str, Any]) -> list[str]:
    lines = ["## Header", f"Date: {header['date'].isoformat()}"]
    for label, key in (("Momentum", "momentum"), ("Research", "research"),
                       ("Short", "short"), ("Baseline", "baseline")):
        snap = header.get(key)
        if snap is None:
            lines.append(f"{label}: n/a (no snapshot yet)")
        else:
            lines.append(f"{label}: equity {_fmt_money(snap['equity'])} at {_fmt_dt(snap['at'])}")
    return lines


def _format_momentum(m: dict[str, Any]) -> list[str]:
    lines = ["## Momentum"]
    lines.append(f"Daily cycle ran: {'yes' if m['cycle_ran'] else 'no'}")

    if m["universe"] is not None:
        u = m["universe"]
        lines.append(
            f"Universe: {u['checked']} checked, {u['fetch_failures']} fetch failure(s), "
            f"{u['candidates']} candidate(s)"
        )
    else:
        lines.append("Universe: no diagnostics today")
    if m["universe_blind"]:
        lines.append("UNIVERSE BLIND today")

    ad = m["analyzed_decided"]
    bv = ad["by_verdict"]
    lines.append(
        f"Analyzed -> decided: {ad['total']} "
        f"(BUY {len(bv['BUY'])}, HOLD {len(bv['HOLD'])}, SELL {len(bv['SELL'])})"
    )
    if bv["BUY"]:
        lines.append(f"  BUY: {', '.join(bv['BUY'])}")
    if bv["SELL"]:
        lines.append(f"  SELL: {', '.join(bv['SELL'])}")

    buys_txt = f" ({', '.join(m['buys_filled'])})" if m["buys_filled"] else ""
    lines.append(f"Buys filled: {len(m['buys_filled'])}{buys_txt}")

    lines.append(f"Rejected: {len(m['rejected'])}")
    for r in m["rejected"]:
        lines.append(f"  {r['symbol']}: {r['reason']}")

    lines.append(f"Exits: {len(m['exits'])}")
    for e in m["exits"]:
        lines.append(f"  {e['symbol']} ({e['rule']})")

    if m["day_equity"] is not None:
        pnl_txt = _fmt_pct(m["day_pnl_pct"])
        suffix = f" ({pnl_txt})" if pnl_txt is not None else ""
        lines.append(f"Day equity: {_fmt_money(m['day_equity'])}{suffix}")
    else:
        lines.append("Day equity: n/a")

    return lines


def _format_research(r: dict[str, Any]) -> list[str]:
    lines = ["## Research"]

    if r["memos"]:
        lines.append(f"Memos written today: {len(r['memos'])}")
        for memo in r["memos"]:
            lines.append(
                f"  {memo['ticker']} ({memo['thesis_type']}, {memo['tier']}, {memo['status']})"
            )
    else:
        lines.append("Memos written today: 0")

    monitor = r["monitor"]
    counts = monitor["counts"]
    if counts is not None:
        lines.append(
            f"Monitor run: {counts['memos_checked']} checked, "
            f"{counts['falsifiers_evaluated']} falsifier(s) evaluated, "
            f"{counts['tripped']} tripped, {counts['escalations']} escalation(s)"
        )
    else:
        lines.append("Monitor run: none today")
    for label, key in (
        ("Falsifiers tripped", "tripped"), ("Escalations", "escalations"),
        ("Resolution due", "resolution_due"), ("Catalyst due", "catalyst_due"),
    ):
        names = monitor[key]
        lines.append(f"{label}: {', '.join(names) if names else 'none'}")

    trades = r["trades"]
    if trades is not None:
        lines.append(
            f"Trades: entered {', '.join(trades['entered']) or 'none'}; "
            f"exited {', '.join(trades['exited']) or 'none'}; "
            f"skipped {', '.join(trades['skipped']) or 'none'}; "
            f"equity {_fmt_money(trades['equity'])}, cash {_fmt_money(trades['cash'])}"
        )
    else:
        lines.append("Trades: no research_trade_run today")

    if r["positions_opened"]:
        lines.append(
            "Positions opened: "
            + ", ".join(f"{p['symbol']} ({p['tier']})" for p in r["positions_opened"])
        )
    if r["positions_closed"]:
        lines.append(
            "Positions closed: "
            + ", ".join(f"{p['symbol']} ({p['reason']})" for p in r["positions_closed"])
        )

    return lines


def _format_short(s: dict[str, Any]) -> list[str]:
    lines = ["## Short sleeve"]
    if not s["configured"]:
        lines.append("Not configured (no short journal).")
        return lines
    if s["trades"] is not None:
        t = s["trades"]
        lines.append(
            f"Trade run: entered {len(t['entered'])}, exited {len(t['exited'])}, "
            f"skipped {len(t['skipped'])}, equity {_fmt_money(t['equity'])}, "
            f"cash {_fmt_money(t['cash'])}"
        )
        for label, key in (("Entered", "entered"), ("Exited", "exited")):
            if t[key]:
                lines.append(f"  {label}: {', '.join(t[key])}")
    else:
        lines.append("Trades: no short_trade_run today")
    if s["overnight"] is not None:
        o = s["overnight"]
        lines.append(
            f"Overnight: screened={'yes' if o['screened'] else 'no'}, "
            f"{o['researched']} memo(s) authored ({o['still_pending']} pending), "
            f"vetted {o['vetted']} (confirmed {o['confirmed']}, rejected {o['rejected']})"
        )
    for label, key in (("Opened", "positions_opened"), ("Closed", "positions_closed")):
        for p in s[key]:
            detail = p.get("tier") or p.get("reason", "")
            lines.append(f"{label}: {p['symbol']} ({detail})")
    return lines


def _format_baseline(b: dict[str, Any]) -> list[str]:
    lines = ["## Baseline"]

    if b["screen"] is not None:
        s = b["screen"]
        lines.append(
            f"Screen run: {s['passers']} passer(s), {len(s['buys'])} buy(s), "
            f"{len(s['exits'])} exit(s), {len(s['skipped'])} skipped, "
            f"equity {_fmt_money(s['equity'])}"
        )
        for label, key in (("Buys", "buys"), ("Exits", "exits"), ("Skipped", "skipped")):
            if s[key]:
                lines.append(f"  {label}: {', '.join(s[key])}")
    else:
        lines.append("Screen run: none today")

    if b["exits"]:
        lines.append(
            "Exits: "
            + ", ".join(f"{e['symbol']} (held {e['held_days']}d)" for e in b["exits"])
        )
    if b["writeoffs"]:
        lines.append(
            "Write-offs: "
            + ", ".join(f"{w['symbol']} ({w['kind']})" for w in b["writeoffs"])
        )

    return lines


def _format_anomalies(anomalies: list[dict[str, Any]]) -> list[str]:
    lines = ["## Anomalies"]
    if not anomalies:
        lines.append("none")
        return lines
    for a in anomalies:
        lines.append(f"  {a['kind']} at {a['at'].isoformat()}")
    return lines


def format_daily_overview(report: dict[str, Any]) -> str:
    """Markdown rendering of build_daily_overview's dict (the CLI/notify
    renderer's only job) — every section always renders; a quiet day adds a
    banner rather than omitting sections, so the skeleton is always visible."""
    lines = [f"# Daily overview -- {report['date'].isoformat()}"]
    if report["quiet"]:
        lines.append("")
        lines.append("Quiet day -- no activity.")
    lines.append("")
    lines += _format_header(report["header"])
    lines.append("")
    lines += _format_momentum(report["momentum"])
    lines.append("")
    lines += _format_research(report["research"])
    lines.append("")
    lines += _format_short(report["short"])
    lines.append("")
    lines += _format_baseline(report["baseline"])
    lines.append("")
    lines += _format_anomalies(report["anomalies"])
    return "\n".join(lines)


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def overview_headline(report: dict[str, Any]) -> str:
    """One line for the push notification: date + key cross-sleeve counts."""
    date_str = report["date"].isoformat()
    momentum = report["momentum"]
    research = report["research"]

    buys = len(momentum["buys_filled"])
    exits = len(momentum["exits"])
    memos = len(research["memos"])
    trips = len(research["monitor"]["tripped"])

    equity = momentum["day_equity"]
    if equity is not None:
        pnl_txt = _fmt_pct(momentum["day_pnl_pct"])
        suffix = f" ({pnl_txt})" if pnl_txt is not None else ""
        equity_str = f"equity ${equity:,.0f}{suffix}"
    else:
        equity_str = "equity n/a"

    return (
        f"{date_str}: momentum {_plural(buys, 'buy')}/{_plural(exits, 'exit')}, "
        f"research {_plural(memos, 'memo')}/{_plural(trips, 'trip')}, "
        f"{equity_str}"
    )
