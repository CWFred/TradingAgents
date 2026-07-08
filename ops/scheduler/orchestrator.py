"""Orchestrator tick handler — called by APScheduler at :00/:30 during trading hours."""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, time, timezone
from decimal import Decimal

from ops import events
from ops.broker.base import BrokerError, NoSuchPosition, OrderRejected
from ops.exits import evaluate_exits
from ops.live_gate import count_live_buy_fills
from ops.trading_time import (
    TRADING_TZ,
    trading_day_start,
    trading_days_back,
    trading_week_start,
)
from ops.universe.filters import apply_deny_list
from ops.universe.momentum import (
    fetch_closes_and_volumes_from_yfinance,
    find_momentum_leaders,
)
from ops.universe.sp500 import load_sp500_members
from ops.universe import yf_pacing


class Orchestrator:
    def __init__(
        self, *, broker, universe_builder, strategy, pipeline_adapter,
        calendar, journal, config,
        members_loader=load_sp500_members,
        momentum_finder=find_momentum_leaders,
        closes_fetch=fetch_closes_and_volumes_from_yfinance,
        now_fn: Callable[[], datetime] | None = None,
    ) -> None:
        self._broker = broker
        self._universe_builder = universe_builder
        self._strategy = strategy
        self._pipeline_adapter = pipeline_adapter
        self._calendar = calendar
        self._journal = journal
        self._config = config
        self._members_loader = members_loader
        self._momentum_finder = momentum_finder
        self._closes_fetch = closes_fetch
        self._now_fn = now_fn if now_fn is not None else lambda: datetime.now(timezone.utc)

    def tick(self) -> None:
        try:
            self._tick_impl()
        except Exception as exc:
            self._journal.record_event(
                events.KIND_ORCHESTRATOR_TICK_ERROR,
                events.orchestrator_tick_error_payload(
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )

    def _tick_impl(self) -> None:
        if not self._calendar.is_open_now():
            return
        self._maybe_snapshot_equity()
        if self._is_daily_halted() or self._is_weekly_halted():
            return
        now = self._now_fn()
        asof_date = now.date()

        # The leaderboard/exits/entries cycle costs ~500 yfinance calls plus
        # up to daily_analysis_budget LLM runs, so it may run only once per
        # trading day — the FIRST open, un-halted tick. Record the event
        # BEFORE running the cycle (not after): a mid-cycle crash must not
        # re-spend the budget on the next tick. Exits crashing mid-cycle is
        # already journaled separately, and the guardian still enforces
        # stops regardless of whether this cycle ran.
        if self._journal.has_event_today(events.KIND_DAILY_CYCLE_RUN, now=now):
            return
        self._journal.record_event(
            events.KIND_DAILY_CYCLE_RUN,
            events.daily_cycle_run_payload(asof_date=asof_date),
            at=now,
        )

        # Discard fetch counters accumulated outside this cycle so the
        # diagnostics below describe exactly one day's sweep.
        yf_pacing.snapshot_and_reset()

        # Leaderboard is computed ONCE per tick: the exit engine reads held
        # names' ranks off it and the builder takes its head for entries.
        # A failure here (or anywhere in the exit step) must not kill the
        # tick — buys degrade gracefully, and the guardian still owns stops.
        leaderboard = []
        try:
            eligible = apply_deny_list(self._members_loader(), self._config.deny_list)
            leaderboard = self._momentum_finder(eligible, asof_date=asof_date)
            self._run_exits(leaderboard, asof_date)
        except Exception as exc:
            self._journal.record_event(
                events.KIND_EXIT_CHECK_ERROR,
                events.exit_check_error_payload(
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )

        held = {p.symbol for p in self._broker.get_positions()}
        free_slots = max(0, self._config.max_open_positions - len(held))
        candidates = self._universe_builder(
            asof_date=asof_date, config=self._config,
            held_symbols=frozenset(held), free_slots=free_slots,
            excluded_symbols=self._cooldown_symbols(asof_date),
            momentum_leaders=leaderboard,
        )
        self._emit_universe_diagnostics(asof_date, len(candidates))
        fresh_candidates = [c for c in candidates if c.symbol not in held]
        current_equity = self._broker.get_equity()
        live_cap = self._compute_live_cap()
        # Bracket the analysis batch: a managed local model backend (e.g. ds4)
        # is torn down when the session exits, freeing its resident memory
        # between ticks. Bringing it up is lazy inside propagate().
        with self._pipeline_adapter.session():
            decisions: list = []
            proposals = self._strategy.propose_orders(
                candidates=fresh_candidates,
                pipeline=self._pipeline_adapter,
                current_equity=current_equity,
                asof_date=asof_date,
                live_max_position_cap=live_cap,
                decision_sink=decisions,
            )
            for proposal in proposals:
                try:
                    self._broker.place_order(proposal.order)
                except OrderRejected:
                    continue
                except BrokerError:
                    break
                cand = proposal.candidate
                self._journal.record_event(
                    events.KIND_POSITION_OPENED,
                    events.position_opened_payload(
                        symbol=cand.symbol,
                        source=cand.source.value,
                        entry_date=asof_date,
                        client_order_id=proposal.order.client_order_id,
                        entry_rank=cand.momentum.rank if cand.momentum else None,
                    ),
                )
            for decision in decisions:
                cand = decision.candidate
                self._journal.record_event(
                    events.KIND_ANALYSIS_DECISION,
                    events.analysis_decision_payload(
                        symbol=cand.symbol,
                        decision=decision.pipeline.decision.value,
                        source=cand.source.value,
                        asof=asof_date.isoformat(),
                        rank=cand.momentum.rank if cand.momentum else None,
                    ),
                )

    def _run_exits(self, leaderboard, asof_date) -> None:
        report = evaluate_exits(
            positions=list(self._broker.get_positions()),
            provenance=self._journal.latest_event_payload_by_symbol(
                events.KIND_POSITION_OPENED,
            ),
            leaderboard=leaderboard,
            closes_fetch=self._closes_fetch,
            config=self._config,
            asof_date=asof_date,
        )
        for skip in report.skips:
            self._journal.record_event(
                events.KIND_EXIT_SKIPPED_MISSING_DATA,
                events.exit_skipped_missing_data_payload(
                    symbol=skip.symbol, reason=skip.reason,
                ),
            )
        for symbol in report.unknown_provenance:
            self._journal.record_event(
                events.KIND_EXIT_UNKNOWN_PROVENANCE,
                events.exit_unknown_provenance_payload(symbol=symbol),
            )
        for decision in report.decisions:
            self._journal.record_event(
                events.KIND_EXIT_DECISION,
                events.exit_decision_payload(
                    symbol=decision.symbol, rule=decision.rule,
                    evidence=decision.evidence,
                ),
            )
            try:
                fill = self._broker.close_position(decision.symbol)
            except NoSuchPosition:
                # The position vanished between the decision and the close —
                # e.g. the guardian's stop fired first. Not an error: journal
                # a breadcrumb instead of exit_check_error (which emails).
                self._journal.record_event(
                    events.KIND_EXIT_SKIPPED_MISSING_DATA,
                    events.exit_skipped_missing_data_payload(
                        symbol=decision.symbol,
                        reason="position already closed before exit order (guardian race)",
                    ),
                )
                continue
            except BrokerError as exc:
                # Position still held, condition still true next tick — the
                # engine is idempotent, so journal and move on.
                self._journal.record_event(
                    events.KIND_EXIT_CHECK_ERROR,
                    events.exit_check_error_payload(
                        error=(f"close_position({decision.symbol}) failed: "
                               f"{type(exc).__name__}: {exc}"),
                    ),
                )
                continue
            self._journal.record_event(
                events.KIND_EXIT_ORDER_PLACED,
                events.exit_order_placed_payload(
                    symbol=decision.symbol,
                    client_order_id=fill.client_order_id,
                    rule=decision.rule,
                ),
            )

    def _emit_universe_diagnostics(self, asof_date, candidate_count: int) -> None:
        stats = yf_pacing.snapshot_and_reset()
        fetch_ok = sum(s["ok"] for s in stats.values())
        fetch_failed = sum(s["failed"] for s in stats.values())
        self._journal.record_event(
            events.KIND_UNIVERSE_DIAGNOSTICS,
            events.universe_diagnostics_payload(
                asof_date=asof_date, candidates=candidate_count,
                fetch_ok=fetch_ok, fetch_failed=fetch_failed, by_label=stats,
            ),
        )
        total = fetch_ok + fetch_failed
        # Majority fetch failures = a blind day even if a few candidates
        # survived — a 96%-failed sweep with 2 survivors must still alarm.
        if total > 0 and fetch_failed * 2 > total:
            self._journal.record_event(
                events.KIND_UNIVERSE_BLIND,
                events.universe_blind_payload(
                    asof_date=asof_date, fetch_ok=fetch_ok,
                    fetch_failed=fetch_failed,
                    detail=(f"majority fetch failures "
                            f"({candidate_count} candidate(s) survived)"),
                ),
            )

    def _cooldown_symbols(self, asof_date) -> frozenset[str]:
        since_date = trading_days_back(
            asof_date, self._config.stopout_reentry_cooldown_days,
        )
        # Day boundaries go through trading_day_start (ET midnight), not UTC
        # midnight — see ops/trading_time.py. since_date is an ET-calendar
        # date already, so build it in TRADING_TZ before converting to UTC.
        since = trading_day_start(
            datetime.combine(since_date, time.min, tzinfo=TRADING_TZ)
        )
        return self._journal.event_symbols_since(events.KIND_STOP_HIT, since)

    def _compute_live_cap(self) -> Decimal | None:
        """Return the live-gate position cap, or None when the gate is inactive.

        While the gate is active (live broker, fewer than ``live_fill_gate_count``
        live BUY fills since the flip), proposed BUY notional is clamped to
        ``live_max_position``.
        """
        if self._config.broker_mode != "robinhood":
            return None
        if count_live_buy_fills(self._journal) >= self._config.live_fill_gate_count:
            return None
        return self._config.live_max_position

    def _maybe_snapshot_equity(self) -> None:
        now = self._now_fn()
        start_of_day = trading_day_start(now)
        existing_day = self._journal.get_latest_equity_snapshot(
            kind="open_day", since=start_of_day,
        )
        if existing_day is None:
            self._journal.record_equity_snapshot(
                kind="open_day",
                equity=self._broker.get_equity(),
                cash=self._broker.get_cash(),
                at=now,
            )
        # Weekly snapshot at first tick of the week.
        monday = trading_week_start(now)
        existing_week = self._journal.get_latest_equity_snapshot(
            kind="open_week", since=monday,
        )
        if existing_week is None:
            self._journal.record_equity_snapshot(
                kind="open_week",
                equity=self._broker.get_equity(),
                cash=self._broker.get_cash(),
                at=now,
            )

    def _is_daily_halted(self) -> bool:
        return self._journal.has_event_today(events.KIND_DAILY_HALT)

    def _is_weekly_halted(self) -> bool:
        return self._journal.has_event_since_last_monday(events.KIND_KILL_SWITCH)
