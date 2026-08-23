import uuid

import pytest

from app.execution.dual_leg_quote import LegSnapshot, compute_dual_leg_quote

DEEP_ASKS = [(0.00005461, 500_000_000.0), (0.00005470, 500_000_000.0)]
DEEP_BIDS = [(0.00005500, 500_000_000.0), (0.00005490, 500_000_000.0)]


def _buy_leg(**overrides):
    base = dict(
        exchange="binance",
        side="buy",
        best_bid=0.00005440,
        best_ask=0.00005461,
        depth_levels=DEEP_ASKS,
        min_qty=1.0,
        step_size=1.0,
        tick_size=0.00000001,
        min_notional=5.0,
        tradable=True,
        maker_fee_rate=0.001,
        taker_fee_rate=0.001,
        fee_source="real_account_fee",
        fetch_started_at=100.0,
        fetch_completed_at=100.05,
    )
    base.update(overrides)
    return LegSnapshot(**base)


def _sell_leg(**overrides):
    base = dict(
        exchange="bybit",
        side="sell",
        best_bid=0.00005500,  # higher than buy's ask -> profitable spread
        best_ask=0.00005520,
        depth_levels=DEEP_BIDS,
        min_qty=100.0,
        step_size=1.0,
        tick_size=0.00000001,
        min_notional=1.0,
        tradable=True,
        maker_fee_rate=0.001,
        taker_fee_rate=0.001,
        fee_source="real_account_fee",
        fetch_started_at=100.1,
        fetch_completed_at=100.15,
    )
    base.update(overrides)
    return LegSnapshot(**base)


def test_dual_leg_quote_executable_with_healthy_spread():
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(),
        sell_leg=_sell_leg(),
        master_requested_size_usd=500.0,
        micro_live_cap_usdt=10.0,
        now=100.2,
    )
    assert quote.buy_exchange == "binance"
    assert quote.sell_exchange == "bybit"
    assert quote.executable is True
    assert quote.net_profit_usd > 0
    assert quote.gross_spread_pct > 0


def test_dual_leg_quote_never_sizes_above_micro_live_cap():
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(),
        sell_leg=_sell_leg(),
        master_requested_size_usd=10_000.0,  # far more than the cap
        micro_live_cap_usdt=10.0,
        now=100.2,
    )
    assert quote.executable_qty * quote.buy_execution_price <= 10.0 + 1e-6


def test_dual_leg_quote_measures_dual_leg_latency_from_real_timestamps():
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(fetch_completed_at=100.0),
        sell_leg=_sell_leg(fetch_completed_at=100.3),
        master_requested_size_usd=10.0,
        micro_live_cap_usdt=10.0,
        now=100.4,
    )
    assert quote.dual_leg_latency_ms == pytest.approx(300.0)
    assert quote.buy_quote_age_ms == pytest.approx(400.0)
    assert quote.sell_quote_age_ms == pytest.approx(100.0)


def test_dual_leg_quote_rejects_when_sell_leg_not_tradable():
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(),
        sell_leg=_sell_leg(tradable=False),
        master_requested_size_usd=10.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.executable is False
    assert "not tradable" in quote.reason


def test_dual_leg_quote_rejects_below_min_notional_on_either_leg():
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(min_notional=1_000_000.0),  # unreachable at 10 USDT cap
        sell_leg=_sell_leg(),
        master_requested_size_usd=10.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.executable is False
    assert quote.buy_min_notional_pass is False


def test_dual_leg_quote_rejects_when_spread_does_not_cover_both_legs_real_costs():
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(best_ask=0.00005440, taker_fee_rate=0.01),  # expensive fee, thin spread
        sell_leg=_sell_leg(best_bid=0.00005441, taker_fee_rate=0.01),
        master_requested_size_usd=10.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.executable is False
    assert quote.net_profit_usd <= 0


def test_dual_leg_quote_flags_insufficient_depth_as_high_slippage():
    thin_asks = [(0.00005461, 1.0)]
    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_leg=_buy_leg(depth_levels=thin_asks, min_qty=1.0, min_notional=0.0),
        sell_leg=_sell_leg(),
        master_requested_size_usd=10.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.buy_slippage_pct >= 100.0
