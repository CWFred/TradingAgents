"""Orchestrator tick handler — called by APScheduler at :00/:30 during trading hours."""
from __future__ import annotations

from datetime import datetime, timezone

from ops.broker.base import BrokerError, OrderRejected


class Orchestrator:
    def __init__(
        self, *, broker, universe_builder, strategy, pipeline_adapter,
        calendar, journal, config,
    ) -> None:
        self._broker = broker
        self._universe_builder = universe_builder
        self._strategy = strategy
        self._pipeline_adapter = pipeline_adapter
        self._calendar = calendar
        self._journal = journal
        self._config = config

    def tick(self) -> None:
        try:
            self._tick_impl()
        except Exception as exc:
            self._journal.record_event(
                "orchestrator_tick_error",
                {"error": f"{type(exc).__name__}: {exc}"},
            )

    def _tick_impl(self) -> None:
        if not self._calendar.is_open_now():
            return
        self._maybe_snapshot_equity()
        if self._is_daily_halted() or self._is_weekly_halted():
            return
        universe = self._universe_builder.build()
        held = {p.symbol for p in self._broker.get_positions()}
        candidates = self._strategy.rank(universe - held)
        for candidate in candidates:
            decision = self._pipeline_adapter.propagate(candidate.symbol, self._today())
            if decision.action != "BUY":
                continue
            order = self._strategy.build_order(candidate, decision)
            try:
                self._broker.place_order(order)
            except OrderRejected:
                continue
            except BrokerError:
                break

    def _maybe_snapshot_equity(self) -> None:
        now = datetime.now(timezone.utc)
        start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
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
        weekday = now.weekday()
        monday = now - _days(weekday)
        monday = monday.replace(hour=0, minute=0, second=0, microsecond=0)
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
        return self._journal.has_event_today("daily_halt")

    def _is_weekly_halted(self) -> bool:
        return self._journal.has_event_since_last_monday("kill_switch")

    def _today(self) -> str:
        return datetime.now(timezone.utc).date().isoformat()


def _days(n: int):
    from datetime import timedelta
    return timedelta(days=n)
