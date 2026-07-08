"""Computed arithmetic for resolving a memo (Phase D, build-order step 7).

Resolution is the one step in the memo lifecycle that must stay a human
judgment call — the outcome label (right/wrong process crossed with
made/lost money) and the narrative are not machine-derivable. Everything
else *is* arithmetic: the exit price, the realized return, the benchmark
return over the identical window, and the holding period. This module
computes exactly that, so `ops research resolve` leaves the human exactly
two inputs.

Exit-price fallback ladder (first hit wins):

  1. an explicit ``--exit-price`` (the caller knows better — an OTC fill, a
     tender settlement, a deal price not yet reflected in a broker fill);
  2. the research journal's last SELL fill for the ticker (the normal path
     — ``ops research trade`` closed the position mechanically);
  3. the current close, via ``price_fetcher``.

Shadow-tracked "passed" memos never generate a fill (the position was never
opened), so per the schema's documented convention their resolved
``exit_price`` is unconditionally ``None`` — even if the caller supplies an
explicit ``--exit-price`` — even though the *return math* still uses
whatever price the ladder above found (the explicit price, a fill, or the
current close). The corpus records "what would have happened," not a
market execution that never occurred.
"""

from __future__ import annotations

from datetime import datetime, timezone

BENCHMARK_SYMBOL = "IWM"  # Russell 2000 proxy for the small/mid-cap universe
# (the schema docstring says "e.g. Russell 2000 Value" — swapping to a
# different index-tracking ticker is a one-constant change here).


class ResolutionError(Exception):
    """Resolution arithmetic could not be completed (missing price data)."""


def _last_sell_fill_price(research_journal, ticker: str):
    """Most recent SELL fill price for ``ticker``, or None.

    ``Journal`` only exposes a last-BUY-fill reader (``last_buy_fill_for`` —
    used by the trade step's equity fallback); there is no matching sell
    reader, so this scans the public ``read_fills()`` list instead. Fills
    are ordered oldest-first by that API, so the last matching entry is the
    most recent one.
    """
    matches = [
        fill for fill in research_journal.read_fills()
        if fill["symbol"].upper() == ticker.upper() and fill["side"] == "SELL"
    ]
    if not matches:
        return None
    # Select by filled_at, not insertion/row-id order (mirrors Journal.
    # last_buy_fill_for's "ORDER BY filled_at DESC" precedent).
    latest = max(matches, key=lambda fill: fill["filled_at"])
    return latest["price"]


def compute_resolution_numbers(
    memo,
    *,
    research_journal,
    price_fetcher=None,
    now: datetime | None = None,
    exit_price: float | None = None,
) -> dict:
    """Everything a ``Resolution`` needs except ``outcome_label``/``narrative``.

    Returns ``{"resolved_at", "exit_price", "realized_return_pct",
    "benchmark_return_pct", "holding_days"}`` — the caller (the CLI) merges
    in the two human-supplied fields and constructs the ``Resolution``.
    """
    if price_fetcher is None:
        from ops.research.prices import fetch_price_context

        price_fetcher = fetch_price_context
    now = now or datetime.now(timezone.utc)
    today = now.date()

    resolved_exit_price: float | None = exit_price
    if resolved_exit_price is None:
        fill_price = _last_sell_fill_price(research_journal, memo.ticker)
        if fill_price is not None:
            resolved_exit_price = float(fill_price)

    # exit_for_return_math is what the arithmetic below actually uses; it is
    # only ever allowed to diverge from the reported exit_price in the one
    # documented case (a passed memo, where the return math uses whatever
    # price the ladder found but the reported exit_price is always None).
    exit_for_return_math = resolved_exit_price
    if resolved_exit_price is None:
        # Ladder step 3: current close. Neither an explicit price nor a fill
        # was found, so this is the only branch that ever needs a live quote.
        price_ctx = price_fetcher(memo.ticker)
        close = price_ctx.close_on_or_before(today) if price_ctx is not None else None
        if close is None:
            raise ResolutionError(
                f"no price data for {memo.ticker} on or before {today}"
            )
        current_close = float(close)
        exit_for_return_math = current_close
        resolved_exit_price = current_close

    # schema.py's Resolution docstring: exit_price is "None for shadow-
    # tracked 'passed' memos" — unqualified. A shadow position has no real
    # exit, so this holds even when the caller passed an explicit
    # --exit-price or a SELL fill exists; the return math above still uses
    # that price.
    if memo.status == "passed":
        resolved_exit_price = None

    realized_return_pct = (
        exit_for_return_math - memo.entry_price_ref
    ) / memo.entry_price_ref

    benchmark_ctx = price_fetcher(BENCHMARK_SYMBOL)
    entry_close = (
        benchmark_ctx.close_on_or_before(memo.as_of_date) if benchmark_ctx is not None else None
    )
    exit_close = (
        benchmark_ctx.close_on_or_before(today) if benchmark_ctx is not None else None
    )
    if entry_close is None or exit_close is None:
        raise ResolutionError(
            f"no benchmark ({BENCHMARK_SYMBOL}) price data spanning "
            f"{memo.as_of_date} to {today}"
        )
    benchmark_return_pct = float((exit_close - entry_close) / entry_close)

    holding_days = (now - memo.created_at).days

    return {
        "resolved_at": now,
        "exit_price": resolved_exit_price,
        "realized_return_pct": float(realized_return_pct),
        "benchmark_return_pct": benchmark_return_pct,
        "holding_days": holding_days,
    }
