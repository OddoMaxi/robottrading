from app.reporting.master_strategy_ranking import StrategyPerformance, _capital_velocity


def test_capital_velocity_none_when_no_capital_used():
    assert _capital_velocity(net_profit_usd=10.0, capital_used_usd=0.0, avg_duration_seconds=60.0) is None


def test_capital_velocity_none_when_no_duration():
    assert _capital_velocity(net_profit_usd=10.0, capital_used_usd=1000.0, avg_duration_seconds=None) is None


def test_capital_velocity_matches_worked_example():
    # $2 profit, $1000 capital, held for exactly 1 minute -> $0.002/capital-minute equivalent scaled by capital: 2 / (1000 * 1) = 0.002
    result = _capital_velocity(net_profit_usd=2.0, capital_used_usd=1000.0, avg_duration_seconds=60.0)
    assert result == 0.002


def test_strategy_performance_is_a_plain_comparable_dataclass():
    a = StrategyPerformance(
        engine="CEX", strategy="cross_exchange", attempts=10, filled=8, net_profit_usd=50.0, capital_used_usd=1000.0,
        avg_duration_seconds=30.0, attempts_per_hour=1.0, filled_per_hour=0.8, profitable_per_hour=0.5,
        capture_rate_pct=80.0, capital_velocity_usd_per_minute=0.1,
    )
    assert a.engine == "CEX"
    assert a.capture_rate_pct == 80.0
