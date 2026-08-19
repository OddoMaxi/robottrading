import pytest

from app.analytics.fees import FeeEngine
from app.execution.fill_probability import estimate_maker_fill_probability
from app.execution.maker_taker import ExecutionMode, best_execution_mode, evaluate_execution_modes


def test_fill_probability_higher_for_tight_spread_and_deep_liquidity():
    tight_deep, _ = estimate_maker_fill_probability(spread_pct=0.01, touch_quantity_usd=10_000, order_size_usd=1_000, recent_volatility_pct=0.05)
    wide_thin, _ = estimate_maker_fill_probability(spread_pct=0.45, touch_quantity_usd=100, order_size_usd=1_000, recent_volatility_pct=0.05)
    assert tight_deep > wide_thin


def test_fill_probability_is_bounded():
    prob, _ = estimate_maker_fill_probability(spread_pct=0.0, touch_quantity_usd=1_000_000, order_size_usd=1, recent_volatility_pct=1.0)
    assert 0.05 <= prob <= 0.95


def test_taker_taker_always_fills_certain():
    fee_engine = FeeEngine()
    results = evaluate_execution_modes(
        "binance", "okx", buy_bid=99_990, buy_ask=100_000, sell_bid=100_300, sell_ask=100_310,
        capital_usd=1_000, fee_engine=fee_engine, buy_maker_fill_probability=0.5, sell_maker_fill_probability=0.5,
    )
    taker_taker = next(r for r in results if r.mode == ExecutionMode.TAKER_TAKER)
    assert taker_taker.fill_probability == pytest.approx(1.0)
    assert taker_taker.expected_value_usd == pytest.approx(taker_taker.net_profit_if_filled_usd)


def test_maker_modes_have_lower_fees_but_execution_risk():
    fee_engine = FeeEngine()
    results = evaluate_execution_modes(
        "binance", "okx", buy_bid=99_990, buy_ask=100_000, sell_bid=100_300, sell_ask=100_310,
        capital_usd=1_000, fee_engine=fee_engine, buy_maker_fill_probability=0.9, sell_maker_fill_probability=0.9,
    )
    by_mode = {r.mode: r for r in results}
    # Maker/Taker buys at the (lower) bid instead of crossing to the ask —
    # better price *if it fills* than Taker/Taker.
    assert by_mode[ExecutionMode.MAKER_TAKER].net_profit_if_filled_usd > by_mode[ExecutionMode.TAKER_TAKER].net_profit_if_filled_usd
    # But it isn't certain, so its probability-weighted expected value is discounted.
    assert by_mode[ExecutionMode.MAKER_TAKER].expected_value_usd < by_mode[ExecutionMode.MAKER_TAKER].net_profit_if_filled_usd


def test_zero_fill_probability_never_wins():
    fee_engine = FeeEngine()
    results = evaluate_execution_modes(
        "binance", "okx", buy_bid=99_990, buy_ask=100_000, sell_bid=100_300, sell_ask=100_310,
        capital_usd=1_000, fee_engine=fee_engine, buy_maker_fill_probability=0.0, sell_maker_fill_probability=0.0,
    )
    best = best_execution_mode(results)
    assert best.mode == ExecutionMode.TAKER_TAKER  # the only mode with any nonzero expected value here
