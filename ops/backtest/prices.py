"""Offline price cache and exchange-session alignment for backtests.

Fetching is deliberately an explicit update operation.  Replay code only uses
the read methods, which makes an accidental network call during a settings
replay impossible.  Provider adjusted prices are stored alongside raw prices;
historical reads rebase the adjusted series at the requested point-in-time so
corporate actions after that date cannot change an older case.
"""
from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Protocol

from ops.backtest.models import PriceBar
from ops.scheduler.market_calendar import MarketCalendar


class PriceBarLike(Protocol):
    symbol: str
    session: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    adjusted_open: Decimal
    adjusted_high: Decimal
    adjusted_low: Decimal
    adjusted_close: Decimal
    volume: Decimal
    dividend: Decimal
    split_ratio: Decimal
    provider: str


CachedPriceBar = PriceBar


class PriceSeriesStatus(str, Enum):
    READY = "ready"
    PENDING = "pending"
    UNPRICEABLE = "unpriceable"
    STALE = "stale"
    TERMINAL = "terminal"


@dataclass(frozen=True)
class PriceSeriesState:
    symbol: str
    status: PriceSeriesStatus
    asof: date | None = None
    reason: str | None = None
    provider: str | None = None
    fetched_at: datetime | None = None


@dataclass(frozen=True)
class NextSessionBar:
    """The required next exchange session and its exact bar, if cached."""

    session_date: date
    bar: PriceBar | None


@dataclass(frozen=True)
class AlignedPriceBar:
    session_date: date
    symbol: PriceBar
    benchmark: PriceBar


@dataclass(frozen=True)
class PriceAlignment:
    pairs: tuple[AlignedPriceBar, ...]
    missing_symbol_sessions: tuple[date, ...]
    missing_benchmark_sessions: tuple[date, ...]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS price_bars (
    symbol TEXT NOT NULL,
    session TEXT NOT NULL,
    open TEXT NOT NULL,
    high TEXT NOT NULL,
    low TEXT NOT NULL,
    close TEXT NOT NULL,
    adjusted_open TEXT NOT NULL,
    adjusted_high TEXT NOT NULL,
    adjusted_low TEXT NOT NULL,
    adjusted_close TEXT NOT NULL,
    volume TEXT NOT NULL,
    dividend TEXT NOT NULL,
    split_ratio TEXT NOT NULL,
    provider TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (symbol, session)
);
CREATE INDEX IF NOT EXISTS idx_price_bars_session
    ON price_bars(session);
CREATE TABLE IF NOT EXISTS price_series_state (
    symbol TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    asof TEXT,
    reason TEXT,
    provider TEXT,
    fetched_at TEXT NOT NULL
);
"""


def _decimal(value: object, *, field: str) -> Decimal:
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except Exception as exc:
        raise ValueError(f"invalid {field}: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"invalid {field}: {value!r}")
    return result


def _coerce_bar(bar: PriceBarLike, *, fetched_at: datetime) -> PriceBar:
    symbol = str(bar.symbol).strip().upper()
    if not symbol:
        raise ValueError("price bar symbol must not be empty")
    session = bar.session
    if not isinstance(session, date):
        raise ValueError(f"invalid session: {session!r}")
    values = {
        name: _decimal(getattr(bar, name), field=name)
        for name in (
            "open", "high", "low", "close", "adjusted_open", "adjusted_high",
            "adjusted_low", "adjusted_close", "dividend", "split_ratio",
        )
    }
    if any(values[name] <= 0 for name in ("open", "high", "low", "close")):
        raise ValueError("raw OHLC values must be positive")
    if any(
        values[name] <= 0
        for name in ("adjusted_open", "adjusted_high", "adjusted_low", "adjusted_close")
    ):
        raise ValueError("adjusted OHLC values must be positive")
    if values["high"] < max(values["open"], values["low"], values["close"]):
        raise ValueError("raw high is below another OHLC value")
    if values["low"] > min(values["open"], values["high"], values["close"]):
        raise ValueError("raw low is above another OHLC value")
    volume = _decimal(bar.volume, field="volume")
    if volume < 0 or values["dividend"] < 0 or values["split_ratio"] <= 0:
        raise ValueError("volume/dividend must be nonnegative and split_ratio positive")
    return PriceBar(
        symbol=symbol,
        session=session,
        volume=volume,
        provider=str(bar.provider),
        fetched_at=fetched_at,
        **values,
    )


class PriceCache:
    """Persistent SQLite cache.  Construction and updates are write paths."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(_SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def upsert_bars(
        self,
        bars: Iterable[PriceBarLike],
        *,
        fetched_at: datetime | None = None,
    ) -> int:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        rows = [_coerce_bar(bar, fetched_at=fetched_at) for bar in bars]
        if not rows:
            return 0
        with self._connect() as conn:
            conn.executemany(
                """
                INSERT INTO price_bars (
                    symbol, session, open, high, low, close,
                    adjusted_open, adjusted_high, adjusted_low, adjusted_close, volume,
                    dividend, split_ratio, provider, fetched_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol, session) DO UPDATE SET
                    open=excluded.open, high=excluded.high, low=excluded.low,
                    close=excluded.close, adjusted_open=excluded.adjusted_open,
                    adjusted_high=excluded.adjusted_high,
                    adjusted_low=excluded.adjusted_low,
                    adjusted_close=excluded.adjusted_close, volume=excluded.volume,
                    dividend=excluded.dividend, split_ratio=excluded.split_ratio,
                    provider=excluded.provider, fetched_at=excluded.fetched_at
                """,
                [
                    (
                        b.symbol, b.session.isoformat(), str(b.open), str(b.high),
                        str(b.low), str(b.close), str(b.adjusted_open), str(b.adjusted_high),
                        str(b.adjusted_low), str(b.adjusted_close), str(b.volume), str(b.dividend),
                        str(b.split_ratio), b.provider, b.fetched_at.isoformat(),
                    )
                    for b in rows
                ],
            )
        return len(rows)

    def update(
        self,
        symbol: str,
        *,
        start: date,
        end: date,
        fetcher: Callable[[str, date, date], Iterable[PriceBarLike]],
        fetched_at: datetime | None = None,
    ) -> int:
        """Fetch explicitly, rejecting provider rows outside the request."""
        if end < start:
            raise ValueError("price update end must be on or after start")
        normalized = symbol.strip().upper()
        supplied = list(fetcher(normalized, start, end))
        for bar in supplied:
            if bar.symbol.strip().upper() != normalized:
                raise ValueError(f"fetcher returned {bar.symbol!r} for {normalized}")
            if not start <= bar.session <= end:
                raise ValueError(
                    f"fetcher returned {bar.session} outside {start}..{end}"
                )
        return self.upsert_bars(supplied, fetched_at=fetched_at)

    def _read_rows(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
    ) -> list[PriceBar]:
        clauses = ["symbol = ?"]
        params: list[object] = [symbol.strip().upper()]
        if start is not None:
            clauses.append("session >= ?")
            params.append(start.isoformat())
        if end is not None:
            clauses.append("session <= ?")
            params.append(end.isoformat())
        sql = "SELECT * FROM price_bars WHERE " + " AND ".join(clauses)
        sql += " ORDER BY session"
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [self._row_to_bar(row) for row in rows]

    @staticmethod
    def _row_to_bar(row: sqlite3.Row) -> PriceBar:
        return PriceBar(
            symbol=row["symbol"],
            session=date.fromisoformat(row["session"]),
            open=Decimal(row["open"]), high=Decimal(row["high"]),
            low=Decimal(row["low"]), close=Decimal(row["close"]),
            adjusted_open=Decimal(row["adjusted_open"]),
            adjusted_high=Decimal(row["adjusted_high"]),
            adjusted_low=Decimal(row["adjusted_low"]),
            adjusted_close=Decimal(row["adjusted_close"]),
            volume=Decimal(row["volume"]), dividend=Decimal(row["dividend"]),
            split_ratio=Decimal(row["split_ratio"]), provider=row["provider"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
        )

    def bars(
        self,
        symbol: str,
        *,
        start: date | None = None,
        end: date | None = None,
        adjusted_to: date | None = None,
        visible_through: date | None = None,
    ) -> list[PriceBar]:
        """Read bars, optionally rebased and point-in-time bounded.

        Provider adjusted series normally reflect every split/dividend known
        today.  Rebase at the latest eligible bar so adjustments after a
        historical case date cancel out, while earlier actions remain in the
        relative history.  ``adjusted_to`` selects that price basis but does
        not hide later bars; use ``visible_through`` for a PIT visibility
        boundary.  Keeping those concepts separate lets a replay observe
        future outcomes in the exact share-price basis used by its frozen
        memo.
        """
        if visible_through is not None:
            end = min(end, visible_through) if end is not None else visible_through
        bars = self._read_rows(symbol, start=start, end=end)
        if adjusted_to is None or not bars:
            return bars
        # The requested slice can end before the adjustment boundary.  Anchor
        # against the latest cached bar at the boundary, not merely the last
        # returned bar, or a split between ``end`` and ``adjusted_to`` would be
        # incorrectly removed from the older slice.
        anchors = self._read_rows(symbol, end=adjusted_to)
        if not anchors:
            raise ValueError(
                f"cannot rebase {symbol.strip().upper()} at {adjusted_to}: "
                "no eligible anchor bar"
            )
        anchor = anchors[-1]
        scalar = anchor.close / anchor.adjusted_close
        return [
            replace(
                bar,
                adjusted_open=bar.adjusted_open * scalar,
                adjusted_high=bar.adjusted_high * scalar,
                adjusted_low=bar.adjusted_low * scalar,
                adjusted_close=bar.adjusted_close * scalar,
            )
            for bar in bars
        ]

    def bar(
        self,
        symbol: str,
        session_date: date,
        *,
        adjusted_to: date | None = None,
        visible_through: date | None = None,
    ) -> PriceBar | None:
        bars = self.bars(
            symbol, start=session_date, end=session_date,
            adjusted_to=adjusted_to, visible_through=visible_through,
        )
        return bars[0] if bars else None

    def set_state(
        self,
        symbol: str,
        status: PriceSeriesStatus,
        *,
        asof: date | None = None,
        reason: str | None = None,
        provider: str | None = None,
        fetched_at: datetime | None = None,
    ) -> None:
        fetched_at = fetched_at or datetime.now(timezone.utc)
        if fetched_at.tzinfo is None:
            raise ValueError("fetched_at must be timezone-aware")
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO price_series_state
                    (symbol, status, asof, reason, provider, fetched_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(symbol) DO UPDATE SET
                    status=excluded.status, asof=excluded.asof,
                    reason=excluded.reason, provider=excluded.provider,
                    fetched_at=excluded.fetched_at
                """,
                (
                    symbol.strip().upper(), status.value,
                    asof.isoformat() if asof else None, reason, provider,
                    fetched_at.isoformat(),
                ),
            )

    def state(self, symbol: str) -> PriceSeriesState | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM price_series_state WHERE symbol = ?",
                (symbol.strip().upper(),),
            ).fetchone()
        if row is None:
            return None
        return PriceSeriesState(
            symbol=row["symbol"], status=PriceSeriesStatus(row["status"]),
            asof=date.fromisoformat(row["asof"]) if row["asof"] else None,
            reason=row["reason"], provider=row["provider"],
            fetched_at=datetime.fromisoformat(row["fetched_at"]),
        )

    def classify(
        self,
        symbol: str,
        *,
        required_through: date,
        calendar: MarketCalendar | None = None,
    ) -> PriceSeriesStatus:
        explicit = self.state(symbol)
        if explicit and explicit.status in {
            PriceSeriesStatus.PENDING,
            PriceSeriesStatus.UNPRICEABLE,
            PriceSeriesStatus.TERMINAL,
        }:
            return explicit.status
        bars = self.bars(symbol, end=required_through)
        if not bars:
            return PriceSeriesStatus.UNPRICEABLE
        required = previous_session_on_or_before(required_through, calendar=calendar)
        if required is not None and bars[-1].session < required:
            return PriceSeriesStatus.STALE
        return PriceSeriesStatus.READY


def next_session_after(
    observed_on: date,
    *,
    calendar: MarketCalendar | None = None,
    max_days: int = 14,
) -> date:
    """First NYSE session strictly after a decision date."""
    calendar = calendar or MarketCalendar()
    for offset in range(1, max_days + 1):
        candidate = observed_on + timedelta(days=offset)
        if calendar.is_trading_day(candidate):
            return candidate
    raise RuntimeError(f"no exchange session within {max_days} days after {observed_on}")


def previous_session_on_or_before(
    day: date,
    *,
    calendar: MarketCalendar | None = None,
    max_days: int = 14,
) -> date | None:
    calendar = calendar or MarketCalendar()
    for offset in range(max_days + 1):
        candidate = day - timedelta(days=offset)
        if calendar.is_trading_day(candidate):
            return candidate
    return None


def exchange_sessions(
    start: date,
    end: date,
    *,
    calendar: MarketCalendar | None = None,
) -> list[date]:
    if end < start:
        return []
    calendar = calendar or MarketCalendar()
    days = (end - start).days
    return [
        start + timedelta(days=offset)
        for offset in range(days + 1)
        if calendar.is_trading_day(start + timedelta(days=offset))
    ]


def next_session_bar(
    cache: PriceCache,
    symbol: str,
    observed_on: date,
    *,
    calendar: MarketCalendar | None = None,
    adjusted_to: date | None = None,
) -> NextSessionBar:
    required = next_session_after(observed_on, calendar=calendar)
    return NextSessionBar(
        session_date=required,
        bar=cache.bar(symbol, required, adjusted_to=adjusted_to),
    )


def align_symbol_and_benchmark(
    cache: PriceCache,
    symbol: str,
    benchmark: str,
    *,
    start: date,
    end: date,
    asof: date | None = None,
    calendar: MarketCalendar | None = None,
) -> PriceAlignment:
    """Align exact exchange sessions; gaps are returned, never forward-filled."""
    if asof is not None:
        end = min(end, asof)
    expected = exchange_sessions(start, end, calendar=calendar)
    stock = {
        bar.session: bar
        for bar in cache.bars(
            symbol, start=start, end=end, adjusted_to=asof,
            visible_through=asof,
        )
    }
    bench = {
        bar.session: bar
        for bar in cache.bars(
            benchmark, start=start, end=end, adjusted_to=asof,
            visible_through=asof,
        )
    }
    pairs = tuple(
        AlignedPriceBar(day, stock[day], bench[day])
        for day in expected
        if day in stock and day in bench
    )
    return PriceAlignment(
        pairs=pairs,
        missing_symbol_sessions=tuple(day for day in expected if day not in stock),
        missing_benchmark_sessions=tuple(day for day in expected if day not in bench),
    )
