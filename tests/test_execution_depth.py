import pytest

from app.analytics.execution_depth import compute_depth_adjusted_edge, evaluate_capital_tier
from app.analytics.fees import FeeEngine
from app.config.constants import MarketType
from app.market_data.orderbook import OrderBookLevel

FEE_ENGINE = FeeEngine()


def test_evaluate_capital_tier_matches_the_shared_pricing_formula():
    """Single-level book (no depth walk) — the same VWAP/fee/profit math
    app.engines._shared._price() already does at one fixed size, just
    factored out so it can run at many sizes."""
    ask_levels = [OrderBookLevel(100.0, 100)]
    bid_levels = [OrderBookLevel(100.5, 100)]

    result = evaluate_capital_tier("binance", "okx", ask_levels, bid_levels, FEE_ENGINE, capital_usd=1_000.0)

    quantity = 1_000.0 / 100.0
    buy_fee = FEE_ENGINE.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=False)
    sell_notional = quantity * 100.5
    sell_fee = FEE_ENGINE.trading_fee("okx", MarketType.SPOT, sell_notional, is_maker=False)
    expected_gross = quantity * (100.5 - 100.0)
    expected_net = expected_gross - buy_fee - sell_fee

    assert result.filled_usd == pytest.approx(1_000.0)
    assert result.fully_filled is True
    assert result.net_profit_usd == pytest.approx(expected_net)
    assert result.net_spread_pct == pytest.approx(expected_net / 1_000.0 * 100)


def test_evaluate_capital_tier_no_liquidity_on_either_side_returns_unfilled():
    result = evaluate_capital_tier("binance", "okx", [], [OrderBookLevel(100.5, 100)], FEE_ENGINE, capital_usd=1_000.0)
    assert result.filled_usd == 0.0
    assert result.net_profit_usd == 0.0
    assert result.net_spread_pct == 0.0


def test_evaluate_capital_tier_partial_fill_when_depth_runs_out():
    # Only $50 available at the ask despite a $1,000 target — the exact
    # "must NOT be considered executable at the full intended size" case
    # from the spec's own example.
    ask_levels = [OrderBookLevel(100.0, 0.5)]  # $50 of depth
    bid_levels = [OrderBookLevel(100.5, 100)]

    result = evaluate_capital_tier("binance", "okx", ask_levels, bid_levels, FEE_ENGINE, capital_usd=1_000.0)

    assert result.filled_usd == pytest.approx(50.0)
    assert result.fully_filled is False


def _gradually_degrading_book() -> tuple[list[OrderBookLevel], list[OrderBookLevel]]:
    """Multiple levels with gradually worsening prices on both legs, so net
    % degrades smoothly as size grows — unlike a single-level book (where %
    is flat) or a 2-level cliff (where profit swings from linear-growth to
    a hard wall). This is what actually produces a non-trivial peak in
    ABSOLUTE profit that isn't simply "the smallest size tested"."""
    ask_levels = [
        OrderBookLevel(100.00, 1.0),
        OrderBookLevel(100.10, 1.5),
        OrderBookLevel(100.30, 3.5),
        OrderBookLevel(100.80, 10.0),
        OrderBookLevel(101.80, 30.0),
        OrderBookLevel(103.50, 100.0),
    ]
    bid_levels = [
        OrderBookLevel(100.50, 1.0),
        OrderBookLevel(100.35, 1.5),
        OrderBookLevel(100.05, 3.5),
        OrderBookLevel(99.50, 10.0),
        OrderBookLevel(98.50, 30.0),
        OrderBookLevel(96.00, 100.0),
    ]
    return ask_levels, bid_levels


def test_optimal_capital_maximizes_absolute_profit_not_percentage():
    """The exact distinction the spec's example makes: a smaller size can
    have a BETTER net %, while a larger size nets MORE real dollars — the
    Capital Allocator needs the dollar-optimal size, not the prettiest %."""
    ask_levels, bid_levels = _gradually_degrading_book()
    edge = compute_depth_adjusted_edge(
        "binance", "okx", ask_levels, bid_levels, gross_spread_pct=0.5, fee_engine=FEE_ENGINE, intended_capital_usd=1_000.0
    )

    tier_100 = next(t for t in edge.tiers if t.capital_usd == 100)
    tier_250 = next(t for t in edge.tiers if t.capital_usd == 250)
    assert tier_100.net_spread_pct > tier_250.net_spread_pct  # $100 has the better %...
    assert tier_250.net_profit_usd > tier_100.net_profit_usd  # ...but $250 nets more real dollars
    assert edge.optimal_capital_usd == 250  # optimal picks the dollar-winner, not the %-winner


def test_theoretical_edge_is_always_the_raw_top_of_book_spread_unaffected_by_depth():
    ask_levels, bid_levels = _gradually_degrading_book()
    edge = compute_depth_adjusted_edge(
        "binance", "okx", ask_levels, bid_levels, gross_spread_pct=0.5, fee_engine=FEE_ENGINE, intended_capital_usd=1_000.0
    )
    assert edge.theoretical_edge_pct == 0.5


def test_depth_adjusted_edge_is_at_the_intended_size_realistic_is_at_the_optimal_size():
    ask_levels, bid_levels = _gradually_degrading_book()
    edge = compute_depth_adjusted_edge(
        "binance", "okx", ask_levels, bid_levels, gross_spread_pct=0.5, fee_engine=FEE_ENGINE, intended_capital_usd=1_000.0
    )
    intended_tier = next(t for t in edge.tiers if t.capital_usd == 1_000.0)
    optimal_tier = next(t for t in edge.tiers if t.capital_usd == edge.optimal_capital_usd)

    assert edge.depth_adjusted_edge_pct == pytest.approx(intended_tier.net_spread_pct)
    assert edge.realistic_executable_edge_pct == pytest.approx(optimal_tier.net_spread_pct)
    # In this fixture the naive intended size (1000) is already past the
    # peak — the whole point of computing an optimal separately from it.
    assert edge.realistic_executable_edge_pct > edge.depth_adjusted_edge_pct


def test_max_profitable_capital_interpolates_beyond_the_optimal_tier():
    ask_levels, bid_levels = _gradually_degrading_book()
    edge = compute_depth_adjusted_edge(
        "binance", "okx", ask_levels, bid_levels, gross_spread_pct=0.5, fee_engine=FEE_ENGINE, intended_capital_usd=1_000.0
    )
    assert edge.max_profitable_capital_usd is not None
    assert edge.optimal_capital_usd < edge.max_profitable_capital_usd < 500  # crosses zero before the next tested tier (500) which is already a loss


def test_no_profitable_tier_at_all_returns_none_optimal_without_fabricating_one():
    # A book so wide the spread never clears fees at any tested size.
    ask_levels = [OrderBookLevel(110.0, 1000)]
    bid_levels = [OrderBookLevel(90.0, 1000)]
    edge = compute_depth_adjusted_edge(
        "binance", "okx", ask_levels, bid_levels, gross_spread_pct=-18.0, fee_engine=FEE_ENGINE, intended_capital_usd=1_000.0
    )
    assert edge.optimal_capital_usd is None
    assert edge.optimal_net_profit_usd is None
    assert edge.max_profitable_capital_usd is None
    assert edge.realistic_executable_edge_pct is None


def test_every_tested_tier_profitable_reports_the_largest_tested_size_not_a_fabricated_ceiling():
    # Deep, flat, cheap book — every standard tier fills entirely within
    # one great price, so nothing in the tested range ever turns a loss.
    ask_levels = [OrderBookLevel(100.0, 1_000_000)]
    bid_levels = [OrderBookLevel(100.5, 1_000_000)]
    edge = compute_depth_adjusted_edge(
        "binance", "okx", ask_levels, bid_levels, gross_spread_pct=0.5, fee_engine=FEE_ENGINE, intended_capital_usd=1_000.0
    )
    largest_tested = max(t.capital_usd for t in edge.tiers)
    assert edge.max_profitable_capital_usd == largest_tested
