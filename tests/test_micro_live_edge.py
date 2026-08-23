from dataclasses import dataclass
from datetime import UTC, datetime

from app.reporting.micro_live_edge import (
    _distribution_stats,
    _group_stats,
    _percentile,
    _time_slices,
    passes_safety_gate,
    recommend_safety_margin_usd,
)


def test_percentile_matches_known_values():
    values = sorted([10.0, 20.0, 30.0, 40.0, 50.0])
    assert _percentile(values, 0) == 10.0
    assert _percentile(values, 100) == 50.0
    assert _percentile(values, 50) == 30.0


def test_distribution_stats_empty_returns_zero_count_no_crash():
    stats = _distribution_stats([])
    assert stats.count == 0
    assert stats.mean is None


def test_distribution_stats_computes_mean_median_rates():
    values = [-1.0, -0.5, 0.1, 0.2, 0.3, 1.0]
    stats = _distribution_stats(values)
    assert stats.count == 6
    assert stats.mean == sum(values) / 6
    assert stats.positive_rate_pct == 4 / 6 * 100
    assert stats.negative_rate_pct == 2 / 6 * 100
    assert stats.worst == -1.0
    assert stats.best == 1.0


def test_recommend_safety_margin_is_the_population_stdev():
    values = [1.0, 2.0, 3.0, 4.0]
    import statistics

    expected = round(statistics.pstdev(values), 6)
    assert recommend_safety_margin_usd(values) == expected


def test_recommend_safety_margin_zero_for_single_observation():
    assert recommend_safety_margin_usd([5.0]) == 0.0


def test_passes_safety_gate_requires_clearing_the_margin_not_just_zero():
    assert passes_safety_gate(0.001, safety_margin_usd=0.01) is False  # nominally positive but inside the noise band
    assert passes_safety_gate(0.02, safety_margin_usd=0.01) is True


@dataclass
class _FakeRow:
    symbol: str
    strategy: str
    observed_at: datetime
    net_expected_profit_usd: float
    gross_expected_profit_usd: float
    estimated_fees_usd: float
    estimated_slippage_pct: float
    fee_source: str
    min_notional_pass: bool = True
    lot_size_pass: bool = True
    balance_pass: bool = True
    executable: bool = True
    book_spread_pct: float = 0.01
    available_depth_usd: float = 100.0


def _row(symbol="BTCUSDT", net=0.01, fee_source="real_binance_fee", minute=0):
    return _FakeRow(
        symbol=symbol,
        strategy="cross_exchange",
        observed_at=datetime(2026, 8, 23, 3, minute, tzinfo=UTC),
        net_expected_profit_usd=net,
        gross_expected_profit_usd=net + 0.01,
        estimated_fees_usd=0.005,
        estimated_slippage_pct=0.1,
        fee_source=fee_source,
    )


def test_group_stats_splits_by_symbol_and_computes_real_fee_coverage():
    rows = [_row("BTCUSDT", net=0.01), _row("BTCUSDT", net=-0.02), _row("ETHUSDT", net=0.03, fee_source="estimated_default")]
    groups = _group_stats(rows, lambda r: r.symbol)
    by_key = {g.key: g for g in groups}
    assert by_key["BTCUSDT"].observations == 2
    assert by_key["BTCUSDT"].real_fee_coverage_pct == 100.0
    assert by_key["ETHUSDT"].real_fee_coverage_pct == 0.0
    assert by_key["ETHUSDT"].observations == 1


def test_time_slices_buckets_observations_by_interval():
    rows = [_row(minute=0), _row(minute=5), _row(minute=35)]
    slices = _time_slices(rows, slice_minutes=30.0)
    assert len(slices) == 2
    assert slices[0].observations == 2
    assert slices[1].observations == 1
