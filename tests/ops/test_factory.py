"""Tests for the public factory and the privacy/concurrency hardening
that prevent guardrails from being bypassed."""
from __future__ import annotations

import threading
from decimal import Decimal

import pytest

from ops import (
    build_default_rule_chain,
    build_guarded_paper_broker,
    build_guarded_robinhood_broker,
)
from ops.broker.base import OrderRejected
from ops.broker.guarded import GuardedBroker
from ops.broker.types import Order, OrderType, Side
from ops.config import OpsConfig
from ops.journal import Journal


@pytest.fixture
def config():
    return OpsConfig()


@pytest.fixture
def journal(tmp_path):
    return Journal(str(tmp_path / "j.sqlite"))


def _factory(tmp_path, *, starting_cash="250", quotes=None):
    journal = Journal(str(tmp_path / "j.sqlite"))
    quotes = quotes or {"AAPL": Decimal("200")}
    cfg = OpsConfig()
    guarded = build_guarded_paper_broker(
        config=cfg,
        journal=journal,
        quote_source=lambda s: quotes[s],
        starting_cash=Decimal(starting_cash),
        start_of_day_equity=lambda: Decimal(starting_cash),
        start_of_week_equity=lambda: Decimal(starting_cash),
    )
    return journal, guarded


def _buy(symbol="AAPL", notional="25", stop_pct="-0.08", cid="c1") -> Order:
    return Order(
        client_order_id=cid, symbol=symbol, side=Side.BUY,
        notional_dollars=Decimal(notional), order_type=OrderType.MARKET,
        stop_pct=Decimal(stop_pct) if stop_pct else None,
    )


def test_factory_returns_guarded_broker(tmp_path):
    _, guarded = _factory(tmp_path)
    assert isinstance(guarded, GuardedBroker)


def test_factory_default_chain_rejects_spot(tmp_path):
    _, guarded = _factory(tmp_path)
    with pytest.raises(OrderRejected) as exc:
        guarded.place_order(_buy(symbol="SPOT"))
    assert exc.value.rule_name == "DenyListRule"


def test_factory_default_chain_allows_normal_order(tmp_path):
    _, guarded = _factory(tmp_path)
    fill = guarded.place_order(_buy())
    assert fill.symbol == "AAPL"


def test_inner_broker_is_name_mangled(tmp_path):
    """A naive bypass attempt via the conventional `_inner` name fails."""
    _, guarded = _factory(tmp_path)
    with pytest.raises(AttributeError):
        guarded._inner  # noqa: B018 — intentional access for the test


def test_default_rule_chain_has_all_thirteen_rules():
    rules = build_default_rule_chain(
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )
    names = [type(r).__name__ for r in rules]
    expected = [
        "DenyListRule", "NoMarginRule", "NoOptionsRule", "NoCryptoRule",
        "LongOnlyRule", "StopAttachedRule", "FractionalSharesOnlyRule",
        "PerTradeDollarFloorRule", "PerPositionCapRule",
        "MaxOpenPositionsRule", "CashReserveRule",
        "DailyDrawdownRule", "WeeklyDrawdownRule",
    ]
    assert names == expected


def test_broker_layer_exception_is_journaled(tmp_path):
    """If the inner broker rejects after guardrails pass, the rejection must
    still land in the journal.

    LongOnlyRule (M5) now closes the old "SELL with no/insufficient position"
    gap this test used to exploit — that exact scenario is caught by the
    guardrail chain today, by design. What's still not closeable by a
    pre-trade rule is a quote read TOCTOU: LongOnlyRule reads its own quote
    to size the sell, and PaperBroker independently re-reads the quote at
    fill time; a price move between those two reads can still make the
    fill-time share count exceed the held quantity. We simulate exactly
    that drift (quote flickers from $200 to $100 between the guardrail's
    read and the broker's fill-time read) to keep exercising the broker-layer
    journaling pathway without relying on a gap the new rule is supposed to
    close."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig()
    quote_calls = {"n": 0}

    def flaky_quote(symbol):
        quote_calls["n"] += 1
        # call #1: BUY fill price. call #2: LongOnlyRule's SELL check.
        # call #3+: PaperBroker's own SELL fill-time price (post-guardrail).
        return Decimal("200") if quote_calls["n"] <= 2 else Decimal("100")

    guarded = build_guarded_paper_broker(
        config=cfg, journal=journal,
        quote_source=flaky_quote,
        starting_cash=Decimal("250"),
        start_of_day_equity=lambda: Decimal("250"),
        start_of_week_equity=lambda: Decimal("250"),
    )
    guarded.place_order(Order(
        client_order_id="cB", symbol="AAPL", side=Side.BUY,
        notional_dollars=Decimal("25"), order_type=OrderType.MARKET,
        stop_pct=Decimal("-0.08"),
    ))
    # BUY fills at $200 (call #1): 25/200 = 0.125 shares held.
    # At $200 (call #2), 20/200 = 0.1 shares <= the 0.125 held —
    # LongOnlyRule passes. At fill time the price has "moved" to $100
    # (call #3), so PaperBroker computes 20/100 = 0.2 shares, which
    # exceeds the 0.125 actually held and raises NoSuchPosition from
    # inside _fill_sell.
    sell = Order(
        client_order_id="cS", symbol="AAPL", side=Side.SELL,
        notional_dollars=Decimal("20"), order_type=OrderType.MARKET,
    )
    with pytest.raises(Exception):
        guarded.place_order(sell)
    broker_rejections = [
        e for e in journal.read_events()
        if e["kind"] == "order_rejected" and e["payload"]["rule"] == "broker"
    ]
    assert len(broker_rejections) == 1
    assert "NoSuchPosition" in broker_rejections[0]["payload"]["reason"]


def test_concurrent_buys_respect_max_open_positions(tmp_path):
    """Two concurrent BUYs against the cap should not both succeed.
    Without the lock, both threads would read the same pre-trade state,
    both pass MaxOpenPositionsRule, and both fill — breaching the cap."""
    journal = Journal(str(tmp_path / "j.sqlite"))
    cfg = OpsConfig(max_open_positions=1)
    quotes = {s: Decimal("200") for s in ("AAPL", "MSFT")}
    guarded = build_guarded_paper_broker(
        config=cfg, journal=journal,
        quote_source=lambda s: quotes[s],
        starting_cash=Decimal("10000"),
        start_of_day_equity=lambda: Decimal("10000"),
        start_of_week_equity=lambda: Decimal("10000"),
    )
    results: list[Exception | None] = [None, None]

    def buy(idx: int, symbol: str) -> None:
        try:
            guarded.place_order(_buy(symbol=symbol, notional="25", cid=f"c{idx}"))
        except OrderRejected as exc:
            results[idx] = exc

    t1 = threading.Thread(target=buy, args=(0, "AAPL"))
    t2 = threading.Thread(target=buy, args=(1, "MSFT"))
    t1.start()
    t2.start()
    t1.join()
    t2.join()

    # Exactly one position should exist; exactly one OrderRejected should have fired.
    assert len(guarded.get_positions()) == 1
    rejections = [r for r in results if isinstance(r, OrderRejected)]
    assert len(rejections) == 1
    assert rejections[0].rule_name == "MaxOpenPositionsRule"


def test_config_rejects_positive_drawdown():
    with pytest.raises(ValueError, match="daily_drawdown_pct"):
        OpsConfig(daily_drawdown_pct=Decimal("0.07"))


def test_config_rejects_positive_weekly_drawdown():
    with pytest.raises(ValueError, match="weekly_drawdown_pct"):
        OpsConfig(weekly_drawdown_pct=Decimal("0.15"))


def test_config_rejects_positive_per_position_stop():
    with pytest.raises(ValueError, match="per_position_stop_pct"):
        OpsConfig(per_position_stop_pct=Decimal("0.08"))


def test_config_rejects_out_of_range_cap():
    with pytest.raises(ValueError, match="per_position_cap_pct"):
        OpsConfig(per_position_cap_pct=Decimal("1.5"))
    with pytest.raises(ValueError, match="per_position_cap_pct"):
        OpsConfig(per_position_cap_pct=Decimal("-0.1"))


def test_config_rejects_zero_or_negative_max_positions():
    with pytest.raises(ValueError, match="max_open_positions"):
        OpsConfig(max_open_positions=0)


def test_config_rejects_unknown_broker_mode():
    with pytest.raises(ValueError, match="broker_mode"):
        OpsConfig(broker_mode="schwab")


def test_config_defaults_still_valid():
    # Default construction must still succeed.
    cfg = OpsConfig()
    assert cfg.broker_mode == "paper"


def test_build_guarded_robinhood_broker_with_fake_client(config, journal):
    from tests.ops.broker.fakes import FakeMCPClient
    client = FakeMCPClient()
    client.set_quote("AAPL", Decimal("10"))
    broker = build_guarded_robinhood_broker(
        config=config, journal=journal,
        mcp_client=client,
        start_of_day_equity=lambda: Decimal("1000"),
        start_of_week_equity=lambda: Decimal("1000"),
    )
    assert isinstance(broker, GuardedBroker)


def test_build_guarded_robinhood_broker_blocks_spot(config, journal):
    from tests.ops.broker.fakes import FakeMCPClient
    client = FakeMCPClient()
    broker = build_guarded_robinhood_broker(
        config=config, journal=journal, mcp_client=client,
        start_of_day_equity=lambda: Decimal("1000"),
        start_of_week_equity=lambda: Decimal("1000"),
    )
    with pytest.raises(OrderRejected):
        broker.place_order(Order(
            client_order_id="b-1", symbol="SPOT", side=Side.BUY,
            notional_dollars=Decimal("50"), order_type=OrderType.MARKET,
            stop_pct=Decimal("-0.08"),
        ))
