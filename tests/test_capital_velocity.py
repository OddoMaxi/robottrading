import pytest

from app.analytics.capital_velocity import capital_velocity_score, return_per_minute


def test_return_per_minute_basic():
    assert return_per_minute(net_return_pct=0.20, holding_time_seconds=60) == pytest.approx(0.20)
    assert return_per_minute(net_return_pct=0.20, holding_time_seconds=120) == pytest.approx(0.10)


def test_small_fast_trade_beats_big_slow_trade():
    """Matches the spec's own worked example (section 14): a $2 profit /
    $1,000 capital / 40s trade should score higher than a $10 profit /
    $5,000 capital / 12h trade, even though the second has 5x the raw profit."""
    score_a, _ = capital_velocity_score(net_profit_usd=10.0, execution_probability=0.9, holding_time_seconds=12 * 3600, capital_usd=5_000.0)
    score_b, _ = capital_velocity_score(net_profit_usd=2.0, execution_probability=0.9, holding_time_seconds=40.0, capital_usd=1_000.0)

    assert score_b > score_a


def test_score_is_bounded_0_to_100():
    score, _ = capital_velocity_score(net_profit_usd=1_000_000, execution_probability=1.0, holding_time_seconds=0.001, capital_usd=1.0)
    assert 0 <= score <= 100

    score_neg, _ = capital_velocity_score(net_profit_usd=-50.0, execution_probability=0.0, holding_time_seconds=1e9, capital_usd=1e9)
    assert 0 <= score_neg <= 100
