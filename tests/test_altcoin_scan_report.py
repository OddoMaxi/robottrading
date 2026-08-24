import statistics
from dataclasses import dataclass
from datetime import UTC, datetime

from app.reporting.altcoin_scan_report import (
    MIN_OBSERVATIONS_TO_JUDGE,
    OpportunityStatus,
    _best_direction_per_symbol,
    _classify,
    _group_by_symbol_and_direction,
    _p10,
    _summarize_direction,
    market_priority_score,
)


def test_classify_no_edge_when_too_few_observations():
    assert _classify(100.0, 10.0, 100.0, observations=2) == OpportunityStatus.NO_EDGE


def test_classify_no_edge_when_never_positive():
    assert _classify(0.0, 0.0, 0.0, observations=20) == OpportunityStatus.NO_EDGE


def test_classify_strong_requires_all_three_conditions():
    assert _classify(80.0, 5.0, 60.0, observations=20) == OpportunityStatus.STRONG


def test_classify_watch_when_positive_but_not_strong():
    assert _classify(50.0, 1.0, 5.0, observations=20) == OpportunityStatus.WATCH


def test_classify_weak_when_rarely_positive():
    assert _classify(10.0, 0.5, 5.0, observations=20) == OpportunityStatus.WEAK


def test_classify_strong_fails_if_persistence_too_short_even_with_good_rate():
    """A spread that flickers positive for a fraction of a second isn't
    STRONG just because it happens often — real execution needs time."""
    assert _classify(90.0, 5.0, 1.0, observations=20) == OpportunityStatus.WATCH


@dataclass
class _FakeRow:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    observed_at: datetime
    gross_spread_pct: float
    net_return_bps: float
    net_profit_per_1000usdt: float
    net_profit_usd: float
    executable: bool
    continuity_status: str
    persistence_seconds: float
    available_depth_usd: float = 500.0


def _row(symbol="ZRO/USDT", buy="binance", sell="bybit", net_profit=1.0, per_1000=5.0, status="new", persistence=10.0, minute=0, depth=500.0):
    return _FakeRow(
        symbol=symbol,
        buy_exchange=buy,
        sell_exchange=sell,
        observed_at=datetime(2026, 8, 23, 18, minute, tzinfo=UTC),
        gross_spread_pct=1.5,
        net_return_bps=50.0,
        net_profit_per_1000usdt=per_1000,
        net_profit_usd=net_profit,
        executable=net_profit > 0,
        continuity_status=status,
        persistence_seconds=persistence,
        available_depth_usd=depth,
    )


def test_summarize_direction_computes_positive_rate_and_best_timestamp():
    rows = [
        _row(net_profit=1.0, per_1000=5.0, status="new", persistence=10.0, minute=0),
        _row(net_profit=2.0, per_1000=8.0, status="continuation", persistence=20.0, minute=1),
        _row(net_profit=-0.5, per_1000=-2.0, status="none", persistence=0.0, minute=2),
    ]
    summary = _summarize_direction(rows)
    assert summary.observations == 3
    assert summary.unique_detections == 1
    assert summary.continuations == 1
    assert summary.positive_rate_pct == 2 / 3 * 100
    assert summary.best_observed_at == datetime(2026, 8, 23, 18, 1, tzinfo=UTC)


def test_p10_falls_back_to_min_with_fewer_than_two_points():
    assert _p10([]) == 0.0
    assert _p10([4.2]) == 4.2


def test_p10_of_a_spread_distribution():
    values = sorted([float(i) for i in range(1, 11)])  # 1..10
    assert _p10(values) == statistics.quantiles(values, n=10, method="inclusive")[0]


def test_summarize_direction_computes_median_p10_and_min_edge():
    rows = [
        _row(net_profit=1.0, per_1000=1.0, minute=0, depth=100.0),
        _row(net_profit=2.0, per_1000=2.0, minute=1, depth=200.0),
        _row(net_profit=3.0, per_1000=10.0, minute=2, depth=300.0),
    ]
    summary = _summarize_direction(rows)
    assert summary.net_profit_per_1000usdt_median == 2.0
    assert summary.net_profit_per_1000usdt_min == 1.0
    assert summary.available_depth_usd_mean == 200.0
    # P10 of [1, 2, 10] should sit near the bottom of the distribution,
    # well below the mean (4.33) — a single lucky tick can't hide here.
    assert summary.net_profit_per_1000usdt_p10 < summary.net_profit_per_1000usdt_mean


def test_group_by_symbol_and_direction_separates_directions():
    rows = [_row(buy="binance", sell="bybit"), _row(buy="bybit", sell="binance")]
    groups = _group_by_symbol_and_direction(rows)
    assert len(groups) == 2


def test_best_direction_per_symbol_picks_the_highest_net_profit_direction():
    rows = [
        _row(symbol="ZRO/USDT", buy="binance", sell="bybit", per_1000=3.0, net_profit=3.0, minute=0),
        _row(symbol="ZRO/USDT", buy="bybit", sell="binance", per_1000=9.0, net_profit=9.0, minute=1),
    ]
    groups = _group_by_symbol_and_direction(rows)
    best = _best_direction_per_symbol(groups)
    assert len(best) == 1
    assert best[0].buy_exchange == "bybit"
    assert best[0].sell_exchange == "binance"


def test_market_priority_score_zero_when_never_persists():
    summary = _summarize_direction([_row(status="none", persistence=0.0, net_profit=0.5, per_1000=1.0)] * MIN_OBSERVATIONS_TO_JUDGE)
    assert market_priority_score(summary, gross_spread_volatility_pct=0.5) == 0.0


def test_market_priority_score_positive_for_a_strong_case():
    rows = [_row(status="new" if i == 0 else "continuation", persistence=30.0, net_profit=3.0, per_1000=4.0, minute=i) for i in range(10)]
    summary = _summarize_direction(rows)
    score = market_priority_score(summary, gross_spread_volatility_pct=0.5)
    assert score > 0.0
