"""Depth-Adjusted Execution Curve (Opportunity Expansion spec, Step 2, user
directive, 2026-08-21).

A single VWAP fill at one fixed size answers "what would this opportunity
net at $1,000" — it can't answer "how much can actually be deployed before
the edge itself erodes to zero" or "what size nets the most real dollars",
since net % isn't monotonic in size: a bigger fill walks deeper into a
thinner part of the book on both legs, so % degrades as size grows even
though the ABSOLUTE profit can still be rising for a while. This module
walks a spread of capital tiers on both legs' real order-book depth and
answers exactly that — the same "$100/$500/$1,000/.../$5,000" curve, an
OPTIMAL size (the tier that nets the most absolute dollars, not the most
generous %), and a MAXIMUM PROFITABLE size (the largest tier still net
of profit, interpolated to the tested tier's own zero-crossing) — the
whole reason a capital allocator must use "optimal", not "as much as is
available".
"""

from dataclasses import dataclass

from app.analytics.fees import FeeEngine
from app.config.constants import MarketType
from app.market_data.orderbook import OrderBookLevel, simulate_vwap

# Same tiers as the (otherwise unwired) Liquidity Engine test amounts
# (app.config.constants.LIQUIDITY_TEST_AMOUNTS_USD, section 12) — reused
# here rather than duplicated, since "how does this look at these standard
# sizes" is the same question both ask.
from app.config.constants import LIQUIDITY_TEST_AMOUNTS_USD as CAPITAL_TEST_TIERS_USD


@dataclass(slots=True)
class DepthTierResult:
    capital_usd: float  # the tier tested, not necessarily what filled
    filled_usd: float  # actually achievable at this tier — < capital_usd once depth runs out
    fully_filled: bool
    net_profit_usd: float
    net_spread_pct: float  # net_profit_usd / filled_usd * 100 — 0.0 if nothing filled


@dataclass(slots=True)
class DepthAdjustedEdge:
    theoretical_edge_pct: float  # top-of-book gross spread %, before any size/cost consideration
    depth_adjusted_edge_pct: float | None  # net spread % at the fixed intended size, before size optimization
    realistic_executable_edge_pct: float | None  # net spread % AT the optimal size — the honest "if we actually traded this, what would we net"
    tiers: list[DepthTierResult]
    optimal_capital_usd: float | None  # the tier maximizing absolute net_profit_usd, among profitable tiers
    optimal_net_profit_usd: float | None
    max_profitable_capital_usd: float | None  # interpolated zero-crossing beyond the largest profitable tier


def evaluate_capital_tier(
    buy_exchange: str,
    sell_exchange: str,
    ask_levels: list[OrderBookLevel],
    bid_levels: list[OrderBookLevel],
    fee_engine: FeeEngine,
    capital_usd: float,
) -> DepthTierResult:
    """Same math app.engines._shared._price() already does at one fixed
    size — factored out here so it can be run at many sizes without
    duplicating the VWAP/fee/profit formula."""
    buy_fill = simulate_vwap(ask_levels, capital_usd)
    sell_fill = simulate_vwap(bid_levels, capital_usd)
    if buy_fill.filled_usd <= 0 or sell_fill.filled_usd <= 0:
        return DepthTierResult(capital_usd, 0.0, False, 0.0, 0.0)

    filled_usd = min(buy_fill.filled_usd, sell_fill.filled_usd)
    quantity = filled_usd / buy_fill.average_price

    buy_fee = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, filled_usd, is_maker=False)
    sell_notional = quantity * sell_fill.average_price
    sell_fee = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, sell_notional, is_maker=False)

    gross_profit = quantity * (sell_fill.average_price - buy_fill.average_price)
    net_profit = gross_profit - buy_fee - sell_fee
    net_spread_pct = (net_profit / filled_usd * 100) if filled_usd else 0.0

    return DepthTierResult(
        capital_usd=capital_usd,
        filled_usd=filled_usd,
        fully_filled=buy_fill.fully_filled and sell_fill.fully_filled,
        net_profit_usd=net_profit,
        net_spread_pct=net_spread_pct,
    )


def _interpolate_zero_crossing(last_profitable: DepthTierResult, first_unprofitable: DepthTierResult) -> float:
    """Linear interpolation, on absolute net_profit_usd, between the
    largest still-profitable tested tier and the next (unprofitable) one —
    an estimate of the exact size where profit crosses back to zero, not
    just "the tier below the one that lost money"."""
    profit_delta = first_unprofitable.net_profit_usd - last_profitable.net_profit_usd
    if profit_delta == 0:
        return last_profitable.capital_usd
    capital_delta = first_unprofitable.capital_usd - last_profitable.capital_usd
    fraction = -last_profitable.net_profit_usd / profit_delta
    return last_profitable.capital_usd + fraction * capital_delta


def compute_depth_adjusted_edge(
    buy_exchange: str,
    sell_exchange: str,
    ask_levels: list[OrderBookLevel],
    bid_levels: list[OrderBookLevel],
    gross_spread_pct: float,
    fee_engine: FeeEngine,
    intended_capital_usd: float,
    test_tiers_usd: list[float] = CAPITAL_TEST_TIERS_USD,
) -> DepthAdjustedEdge:
    # The intended size is always evaluated even if it isn't one of the
    # standard tiers, so depth_adjusted_edge_pct means exactly "at the size
    # this opportunity was actually priced for", not "at the nearest tier".
    tier_sizes = sorted(set(test_tiers_usd) | {intended_capital_usd})
    tiers = [evaluate_capital_tier(buy_exchange, sell_exchange, ask_levels, bid_levels, fee_engine, size) for size in tier_sizes]

    intended_result = next((t for t in tiers if t.capital_usd == intended_capital_usd), None)
    depth_adjusted_edge_pct = intended_result.net_spread_pct if intended_result and intended_result.filled_usd > 0 else None

    profitable_tiers = [t for t in tiers if t.filled_usd > 0 and t.net_profit_usd > 0]
    optimal = max(profitable_tiers, key=lambda t: t.net_profit_usd, default=None)

    max_profitable_capital_usd: float | None = None
    if optimal is not None:
        # Find the smallest tested tier LARGER than the optimal one that
        # turned unprofitable, to interpolate the real crossing point
        # beyond it — not just "some tier lost money somewhere".
        larger_tiers = sorted((t for t in tiers if t.capital_usd > optimal.capital_usd), key=lambda t: t.capital_usd)
        first_unprofitable = next((t for t in larger_tiers if t.filled_usd <= 0 or t.net_profit_usd <= 0), None)
        if first_unprofitable is not None:
            max_profitable_capital_usd = _interpolate_zero_crossing(optimal, first_unprofitable)
        else:
            # Every tested tier up to the largest was still profitable —
            # the true ceiling wasn't reached by the tier list; report the
            # largest tested tier's own size as a floor, not a fabricated
            # extrapolation beyond what was actually simulated.
            max_profitable_capital_usd = larger_tiers[-1].capital_usd if larger_tiers else optimal.capital_usd

    return DepthAdjustedEdge(
        theoretical_edge_pct=gross_spread_pct,
        depth_adjusted_edge_pct=depth_adjusted_edge_pct,
        realistic_executable_edge_pct=optimal.net_spread_pct if optimal else None,
        tiers=tiers,
        optimal_capital_usd=optimal.capital_usd if optimal else None,
        optimal_net_profit_usd=optimal.net_profit_usd if optimal else None,
        max_profitable_capital_usd=max_profitable_capital_usd,
    )
