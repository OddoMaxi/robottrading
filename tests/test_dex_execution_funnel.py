from app.reporting.dex_execution_funnel import _build_funnel


def test_build_funnel_computes_conversion_and_averages():
    detection_row = (100, 20, 90, 80, 80)  # detected, duplicate_economic_event, gross_positive, net_positive, executable
    attempt_row = (80, 60, 10, 3, 8, 2, 45, 15, 8000.0, 120.0, 1500.0)  # attempts, filled, edge_disappeared, not_profitable_at_size, failed, no_capital, profitable, losing, capital_used, total_net_profit, avg_lock_ms
    funnel = _build_funnel("dex_cross", detection_row, attempt_row)

    assert funnel.detected == 100
    assert funnel.duplicate_economic_event == 20
    assert funnel.unique_economic_opportunities == 80
    assert funnel.executable == 80
    assert funnel.attempts == 80
    assert funnel.filled == 60
    assert funnel.not_profitable_at_size == 3
    assert funnel.avg_net_profit_usd == 1.5
    assert funnel.total_net_profit_usd == 120.0
    assert funnel.conversion_pct(funnel.filled, funnel.attempts) == 75.0
    assert funnel.conversion_pct(funnel.executable, funnel.detected) == 80.0


def test_build_funnel_handles_zero_attempts_without_dividing_by_zero():
    detection_row = (10, 0, 5, 2, 2)
    attempt_row = (0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, None)
    funnel = _build_funnel("dex_multihop", detection_row, attempt_row)

    assert funnel.attempts == 0
    assert funnel.avg_net_profit_usd is None
    assert funnel.net_profit_per_capital_minute_usd is None
    assert funnel.conversion_pct(funnel.filled, funnel.attempts) is None


def test_build_funnel_capital_minute_uses_real_lock_time():
    # $1000 capital, held for exactly 1 minute (60,000ms), $2 net profit
    # across 1 trade -> $2 / (1000 * 1 minute) = $0.002/capital-minute.
    detection_row = (1, 0, 1, 1, 1)
    attempt_row = (1, 1, 0, 0, 0, 0, 1, 0, 1000.0, 2.0, 60_000.0)
    funnel = _build_funnel("dex_cross", detection_row, attempt_row)
    assert funnel.net_profit_per_capital_minute_usd == 0.002


def test_build_funnel_none_denominators_return_none_not_a_crash():
    funnel = _build_funnel("atomic", (0, 0, 0, 0, 0), (0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, None))
    assert funnel.conversion_pct(0, 0) is None


def test_build_funnel_unique_economic_opportunities_excludes_duplicates():
    """Reality audit section 2: the raw `detected` count includes both
    twins of a duplicate economic event (both are still persisted, for
    detection transparency), but unique_economic_opportunities must not."""
    detection_row = (596, 179, 400, 300, 300)
    attempt_row = (0, 0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, None)
    funnel = _build_funnel("dex_triangular", detection_row, attempt_row)
    assert funnel.unique_economic_opportunities == 596 - 179
