import pytest

from app.analytics.fees import FeeEngine
from app.analytics.maker_simulation import MakerAssumptions, best_maker_pair, simulate_maker_leg_pair


def test_certain_fill_equals_both_fill_scenario():
    fee_engine = FeeEngine()
    assumptions = MakerAssumptions(fill_probability=1.0, adverse_move_pct=0.05)

    result = simulate_maker_leg_pair("binance", "okx", buy_bid=100_000, sell_ask=100_300, capital_usd=1_000, fee_engine=fee_engine, assumptions=assumptions)

    assert result.probability_both_fill == pytest.approx(1.0)
    assert result.probability_one_leg_fills == pytest.approx(0.0)
    assert result.expected_value_usd == pytest.approx(result.both_fill_profit_usd)


def test_zero_fill_probability_gives_zero_expected_value():
    fee_engine = FeeEngine()
    assumptions = MakerAssumptions(fill_probability=0.0, adverse_move_pct=0.05)

    result = simulate_maker_leg_pair("binance", "okx", buy_bid=100_000, sell_ask=100_300, capital_usd=1_000, fee_engine=fee_engine, assumptions=assumptions)

    assert result.expected_value_usd == pytest.approx(0.0)


def test_wider_spread_increases_both_fill_profit():
    fee_engine = FeeEngine()
    assumptions = MakerAssumptions(fill_probability=0.5, adverse_move_pct=0.05)

    tight = simulate_maker_leg_pair("binance", "okx", buy_bid=100_000, sell_ask=100_050, capital_usd=1_000, fee_engine=fee_engine, assumptions=assumptions)
    wide = simulate_maker_leg_pair("binance", "okx", buy_bid=100_000, sell_ask=100_500, capital_usd=1_000, fee_engine=fee_engine, assumptions=assumptions)

    assert wide.both_fill_profit_usd > tight.both_fill_profit_usd
    assert wide.expected_value_usd > tight.expected_value_usd


def test_best_maker_pair_picks_highest_expected_value():
    fee_engine = FeeEngine()
    assumptions = MakerAssumptions(fill_probability=0.6, adverse_move_pct=0.05)

    quotes = {
        "binance": (100_000, 100_010),
        "okx": (100_300, 100_310),
        "bybit": (99_990, 100_000),
    }

    best = best_maker_pair(quotes, capital_usd=1_000, fee_engine=fee_engine, assumptions=assumptions)

    assert best is not None
    buy_exchange, sell_exchange, result = best
    assert buy_exchange == "bybit"  # lowest bid
    assert sell_exchange == "okx"  # highest ask — widest spread of any pair
    assert result.both_fill_profit_usd > 0  # profitable if both legs actually fill
    # ...but with a 40% chance either leg misses, the naked-exposure cost
    # (adverse move + a taker fee to flatten) drags the expected value
    # negative — this is the real risk the maker approach carries.
    assert result.expected_value_usd < result.both_fill_profit_usd
