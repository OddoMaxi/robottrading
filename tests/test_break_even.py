import pytest

from app.analytics.break_even import compute_break_even
from app.analytics.fees import FeeEngine
from app.config.constants import MarketType


def test_break_even_sums_both_legs_fees_plus_buffers():
    fee_engine = FeeEngine()  # binance taker 0.10%, okx taker 0.10%
    legs = [("binance", MarketType.SPOT, False), ("okx", MarketType.SPOT, False)]

    result = compute_break_even(fee_engine, legs, slippage_buffer_pct=0.02, rebalancing_pct=0.01, safety_margin_pct=0.03)

    assert result.trading_fees_pct == pytest.approx(0.20)  # 0.10% + 0.10%
    assert result.total_pct == pytest.approx(0.26)  # 0.20 + 0.02 + 0.01 + 0.03


def test_maker_legs_have_lower_break_even_than_taker():
    fee_engine = FeeEngine()
    taker_legs = [("binance", MarketType.SPOT, False), ("okx", MarketType.SPOT, False)]
    maker_legs = [("binance", MarketType.SPOT, True), ("okx", MarketType.SPOT, True)]

    taker = compute_break_even(fee_engine, taker_legs)
    maker = compute_break_even(fee_engine, maker_legs)

    assert maker.total_pct < taker.total_pct


def test_triangular_three_legs_break_even_exceeds_two_leg():
    fee_engine = FeeEngine()
    two_legs = [("binance", MarketType.SPOT, False), ("binance", MarketType.SPOT, False)]
    three_legs = two_legs + [("binance", MarketType.SPOT, False)]

    assert compute_break_even(fee_engine, three_legs).total_pct > compute_break_even(fee_engine, two_legs).total_pct
