"""One-shot stop-loss enforcement.

For every open position, check the current quote and place a close-all SELL
if the position is at or past the per_position_stop_pct threshold. This is
the single-pass variant; Plan 3 will wrap it in a background-thread loop."""
from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

from ops import events
from ops.broker.base import BrokerError, QuoteUnavailable
from ops.broker.guarded import GuardedBroker
from ops.config import OpsConfig


@dataclass(frozen=True)
class StopAction:
    symbol: str
    entry: Decimal
    current: Decimal
    pct: Decimal
    sold: bool
    reason: str


class PositionGuardian:
    # Consecutive fully-failed passes (every open position's quote failed)
    # before the guardian escalates a guardian_blind event. A pass with
    # zero open positions is never "fully-failed" (nothing to check).
    _BLIND_STREAK_THRESHOLD = 5

    def __init__(
        self,
        *,
        broker: GuardedBroker,
        quote_source: Callable[[str], Decimal],
        config: OpsConfig,
        journal=None,
        broker_mode: str = "paper",
        market_open_fn: Callable[[], bool] | None = None,
    ):
        self._broker = broker
        self._quote = quote_source
        self._cfg = config
        self._journal = journal if journal is not None else broker.journal
        self._broker_mode = broker_mode
        # None = ungated (ad-hoc/decide-once runs). The always-on service
        # MUST pass the market calendar here: the spec forbids the guardian
        # from trading outside regular hours ("stops breached AH fire at
        # next open"), and after-hours quotes are not executable liquidity.
        self._market_open = market_open_fn
        # Blind-guardian escalation state. This is instance state that
        # persists across polls because the guardian is constructed once in
        # _wire and check_stops_once is called every 60s by the scheduler.
        self._consecutive_blind_passes = 0
        self._blind_alarm_active = False
        # Liveness signal for the dead-man's switch (A1.3): monotonic time
        # of the most recent pass START. None until the first pass, so a
        # never-scheduled guardian correctly looks dead to the heartbeat.
        self.last_pass_started_at: float | None = None

    def check_stops_once(self) -> list[StopAction]:
        """Scheduler-safe wrapper: any unexpected exception is journaled
        as guardian_check_error and swallowed, so the APScheduler job
        keeps running. The guardian is the last line of defence on real
        money — a silent crash here would leave positions unprotected."""
        # FIRST statement, before the try and before the market-hours gate:
        # the heartbeat's liveness question is "is this loop being
        # scheduled?", which overnight/weekend and even crashing passes
        # still answer yes to — only a wedged or dead loop must look dead.
        self.last_pass_started_at = time.monotonic()
        self._touch_liveness()
        try:
            return self._check_stops_once_impl()
        except Exception as exc:
            self._journal.record_event(
                events.KIND_GUARDIAN_CHECK_ERROR,
                events.guardian_check_error_payload(
                    error=f"{type(exc).__name__}: {exc}",
                ),
            )
            return []

    def _touch_liveness(self) -> None:
        # Best-effort by hard rule: the guardian is the last line of
        # defence on real money — no filesystem problem may ever stop a
        # stop-loss pass. getattr: configs constructed before this field
        # existed (old pickles/tests) must not crash the guardian either.
        path = getattr(self._cfg, "guardian_liveness_path", None)
        if not path:
            return
        try:
            p = Path(path)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.touch()
        except OSError:
            pass

    def _check_stops_once_impl(self) -> list[StopAction]:
        if self._market_open is not None and not self._market_open():
            return []
        actions: list[StopAction] = []
        positions = list(self._broker.get_positions())
        quote_failures = 0
        for pos in positions:
            try:
                current = self._quote(pos.symbol)
            except (QuoteUnavailable, BrokerError) as exc:
                # Live RobinhoodBroker.get_quote raises plain BrokerError,
                # not just QuoteUnavailable — catch both so one bad quote
                # skips only this position instead of aborting the pass
                # (it would otherwise escape to check_stops_once's
                # catch-all and discard every remaining position that
                # minute).
                quote_failures += 1
                self._broker.journal.record_event(
                    events.KIND_QUOTE_UNAVAILABLE,
                    events.quote_unavailable_payload(
                        symbol=pos.symbol,
                        context="guardian_stop_check",
                        error=str(exc),
                    ),
                )
                actions.append(StopAction(
                    symbol=pos.symbol,
                    entry=pos.avg_entry_price,
                    current=Decimal("0"),
                    pct=Decimal("0"),
                    sold=False,
                    reason=f"quote unavailable: {exc}",
                ))
                continue

            if pos.stop_loss_price is not None:
                triggered = current <= pos.stop_loss_price
                mode = "absolute"
                threshold_repr = f"abs {pos.stop_loss_price}"
                pct = pos.unrealized_pct(current)
            else:
                pct = pos.unrealized_pct(current)
                triggered = pct <= self._cfg.per_position_stop_pct
                mode = "pct"
                threshold_repr = f"pct {self._cfg.per_position_stop_pct}"

            if not triggered:
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"unrealized {pct} above stop ({mode} {threshold_repr})",
                ))
                continue

            try:
                self._broker.close_position(pos.symbol)
            except BrokerError as exc:
                self._broker.journal.record_event(
                    events.KIND_STOP_FAILED,
                    events.stop_failed_payload(
                        symbol=pos.symbol, entry=pos.avg_entry_price,
                        current=current, pct=pct,
                        mode=mode, threshold_repr=threshold_repr,
                        error=f"{type(exc).__name__}: {exc}",
                    ),
                )
                actions.append(StopAction(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct, sold=False,
                    reason=f"stop-sell failed: {type(exc).__name__}: {exc}",
                ))
                continue

            self._broker.journal.record_event(
                events.KIND_STOP_HIT,
                events.stop_hit_payload(
                    symbol=pos.symbol, entry=pos.avg_entry_price,
                    current=current, pct=pct,
                    mode=mode, threshold_repr=threshold_repr,
                ),
            )
            actions.append(StopAction(
                symbol=pos.symbol, entry=pos.avg_entry_price,
                current=current, pct=pct, sold=True,
                reason=f"stop hit at {pct} ({mode} {threshold_repr})",
            ))
        self._update_blind_streak(total=len(positions), failures=quote_failures)
        self._maybe_trip_kill_switch()
        self._maybe_trip_daily_halt()
        return actions

    def _update_blind_streak(self, *, total: int, failures: int) -> None:
        """Track consecutive fully-failed passes (every open position's
        quote failed) so a persistent live-quote outage escalates even
        though each individual failure is otherwise silently swallowed as
        quote_unavailable.

        A pass with zero open positions has nothing to check: it is
        deliberately excluded from both incrementing AND resetting the
        streak, so an empty book never trips the alarm but also never
        papers over an in-progress outage that started before the book
        emptied out. Any pass that obtains at least one usable quote
        resets the streak. The guardian_blind event fires once per
        crossing of the threshold, not on every pass after.
        """
        if total == 0:
            return
        if failures == total:
            self._consecutive_blind_passes += 1
            if (
                self._consecutive_blind_passes >= self._BLIND_STREAK_THRESHOLD
                and not self._blind_alarm_active
            ):
                self._blind_alarm_active = True
                self._journal.record_event(
                    events.KIND_GUARDIAN_BLIND,
                    events.guardian_blind_payload(
                        consecutive_failed_passes=self._consecutive_blind_passes,
                    ),
                )
        else:
            self._consecutive_blind_passes = 0
            self._blind_alarm_active = False

    def _maybe_trip_kill_switch(self) -> None:
        from datetime import datetime, timezone

        from ops.trading_time import trading_week_start

        tripped = self._journal.has_event_since_last_monday(events.KIND_KILL_SWITCH)
        if not tripped:
            # Baseline must be from THIS week. A stale snapshot (guardian-only
            # mode after a reconcile halt, or a long-idle restart) would
            # compare current equity against weeks-old numbers and can
            # falsely liquidate the whole book.
            now = datetime.now(timezone.utc)
            monday = trading_week_start(now)
            snap = self._journal.get_latest_equity_snapshot(
                kind="open_week", since=monday)
            if snap is None or snap.equity <= 0:
                # No baseline yet this week: record one now (≈ first pass of
                # the week during market hours, i.e. Monday open) and measure
                # from here on subsequent passes.
                self._journal.record_equity_snapshot(
                    kind="open_week",
                    equity=self._broker.get_equity(),
                    cash=self._broker.get_cash(),
                    note="guardian baseline",
                )
                return
            equity_now = self._broker.get_equity()
            weekly_pct = (equity_now - snap.equity) / snap.equity
            if weekly_pct > self._cfg.weekly_drawdown_pct:
                return
            self._journal.record_event(
                events.KIND_KILL_SWITCH,
                events.kill_switch_payload(
                    mode=self._broker_mode,
                    equity_now=equity_now,
                    equity_open_week=snap.equity,
                    pct=weekly_pct,
                    threshold=self._cfg.weekly_drawdown_pct,
                ),
            )
        # Tripped this week — now or on an earlier pass. Paper mode sweeps
        # any positions still open, so an auto-close interrupted by a crash
        # or a transient close failure resumes on the next pass instead of
        # being skipped forever by the idempotency check.
        if self._broker_mode == "paper":
            for pos in list(self._broker.get_positions()):
                try:
                    self._broker.close_position(pos.symbol)
                except Exception as exc:
                    self._journal.record_event(
                        events.KIND_KILL_SWITCH_CLOSE_FAILED,
                        events.kill_switch_close_failed_payload(
                            symbol=pos.symbol,
                            error=f"{type(exc).__name__}: {exc}",
                        ),
                    )

    def _maybe_trip_daily_halt(self) -> None:
        """Computed in the same pass as the weekly kill switch, mirroring its
        snapshot-freshness rule exactly (both use the ET trading-calendar
        boundary from ops.trading_time — see M7). Unlike the kill switch, a
        daily halt never closes positions: it only stops new BUYs, which the
        orchestrator's has_event_today('daily_halt') short-circuit and
        DailyDrawdownRule (order-boundary backstop) already enforce."""
        from datetime import datetime, timezone

        from ops.trading_time import trading_day_start

        if self._journal.has_event_today(events.KIND_DAILY_HALT):
            return
        now = datetime.now(timezone.utc)
        start_of_day = trading_day_start(now)
        snap = self._journal.get_latest_equity_snapshot(
            kind="open_day", since=start_of_day)
        if snap is None or snap.equity <= 0:
            # No baseline yet today: record one now (mirrors the weekly
            # kill-switch fallback) and measure from here on subsequent
            # passes instead of comparing against a missing/stale snapshot.
            self._journal.record_equity_snapshot(
                kind="open_day",
                equity=self._broker.get_equity(),
                cash=self._broker.get_cash(),
                note="guardian baseline",
            )
            return
        equity_now = self._broker.get_equity()
        daily_pct = (equity_now - snap.equity) / snap.equity
        if daily_pct > self._cfg.daily_drawdown_pct:
            return
        self._journal.record_event(
            events.KIND_DAILY_HALT,
            events.daily_halt_payload(
                mode=self._broker_mode,
                equity_now=equity_now,
                equity_open_day=snap.equity,
                pct=daily_pct,
                threshold=self._cfg.daily_drawdown_pct,
            ),
        )
