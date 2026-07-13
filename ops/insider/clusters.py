"""Cluster detection + strength scoring over the insider signal store.

A cluster = >= MIN_BUYERS distinct insiders each with >= MIN_BUY_DOLLARS of
open-market, non-10b5-1 buys inside the rolling CLUSTER_WINDOW_DAYS window.
STRONG when the cluster is bigger, richer, or led by the CEO/CFO; else
BASIC. Names inside their post-entry cooldown are excluded — one cluster =
one entry per quarter, not a drip of re-entries off the same signal.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from ops.insider.store import SignalStore

CLUSTER_WINDOW_DAYS = 30
MIN_BUYERS = 2
MIN_BUY_DOLLARS = Decimal("10000")           # per-insider aggregate in window
STRONG_MIN_BUYERS = 3
STRONG_MIN_AGG_DOLLARS = Decimal("250000")
COOLDOWN_DAYS = 90
_CHIEF_TITLE = re.compile(r"CEO|Chief Executive|CFO|Chief Financial", re.IGNORECASE)


@dataclass(frozen=True)
class Cluster:
    symbol: str
    strength: str  # "BASIC" | "STRONG"
    buyers: tuple[str, ...]
    agg_dollars: Decimal
    accessions: tuple[str, ...]
    latest_buy: date


def find_clusters(store: SignalStore, *, asof: date) -> list[Cluster]:
    since = asof - timedelta(days=CLUSTER_WINDOW_DAYS)
    out: list[Cluster] = []
    for symbol in store.symbols_with_new_buys(since=since):
        last_entry = store.last_entry_date(symbol)
        if last_entry is not None and (asof - last_entry).days < COOLDOWN_DAYS:
            continue
        buys = store.buys_in_window(symbol, since=since, until=asof)
        dollars_by_insider: dict[str, Decimal] = {}
        chief_involved = False
        for b in buys:
            if b["shares"] is None or b["price"] is None:
                continue
            dollars_by_insider[b["insider_name"]] = (
                dollars_by_insider.get(b["insider_name"], Decimal("0"))
                + b["shares"] * b["price"]
            )
            if b["insider_title"] and _CHIEF_TITLE.search(b["insider_title"]):
                chief_involved = True
        qualified = {name: d for name, d in dollars_by_insider.items()
                     if d >= MIN_BUY_DOLLARS}
        if len(qualified) < MIN_BUYERS:
            continue
        agg = sum(qualified.values(), Decimal("0"))
        strength = "STRONG" if (
            len(qualified) >= STRONG_MIN_BUYERS
            or agg >= STRONG_MIN_AGG_DOLLARS
            or chief_involved
        ) else "BASIC"
        qualified_buys = [b for b in buys if b["insider_name"] in qualified]
        out.append(Cluster(
            symbol=symbol, strength=strength,
            buyers=tuple(sorted(qualified)),
            agg_dollars=agg,
            accessions=tuple(sorted({b["accession"] for b in qualified_buys})),
            latest_buy=max(b["transaction_date"] for b in qualified_buys),
        ))
    return out
