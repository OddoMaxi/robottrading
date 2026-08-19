"""What-if analysis: would maker (limit) orders make cross-exchange arbitrage
profitable, given the fill-risk they carry?

Maker orders rest passively — buy at the bid, sell at the ask — instead of
crossing the book like taker orders do. Cheaper fees, and if filled, a wider
captured spread, but no guarantee of a fill at all before the price moves.

We have no real historical fill-rate data for these exchanges (that needs a
testnet/live run — cahier des charges section 36), so `fill_probability` and
`adverse_move_pct` below are explicit, adjustable ASSUMPTIONS, not
measurements. Every result here is conditional on those assumptions, never a
forecast.
"""

from dataclasses import dataclass

from app.analytics.fees import FeeEngine
from app.config.constants import MarketType


@dataclass(slots=True)
class MakerAssumptions:
    # P(a single resting limit order fills before the opportunity closes) — unvalidated.
    fill_probability: float = 0.55
    # % price move against you if forced to flatten a naked leg at market after a partial fill — unvalidated.
    adverse_move_pct: float = 0.05


@dataclass(slots=True)
class MakerScenarioResult:
    both_fill_profit_usd: float
    one_leg_profit_usd: float  # the naked-exposure scenario — usually negative
    probability_both_fill: float
    probability_one_leg_fills: float
    probability_neither_fills: float
    expected_value_usd: float
    expected_value_pct: float


def simulate_maker_leg_pair(
    buy_exchange: str,
    sell_exchange: str,
    buy_bid: float,
    sell_ask: float,
    capital_usd: float,
    fee_engine: FeeEngine,
    assumptions: MakerAssumptions,
) -> MakerScenarioResult:
    """Expected value of a maker-order cross-exchange trade at one point in time.

    Three scenarios, weighted by the (assumed, independent) fill probability
    of each leg:
      - both legs fill: full maker-priced profit, maker fees on both sides.
      - exactly one leg fills: the other has to be flattened at market after
        an assumed adverse move — modeled as a flat cost, not tied to which
        specific leg happened to fail.
      - neither fills: nothing traded, zero cost, zero profit.
    """
    quantity = capital_usd / buy_bid

    buy_fee_maker = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital_usd, is_maker=True)
    sell_notional = quantity * sell_ask
    sell_fee_maker = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, sell_notional, is_maker=True)

    both_fill_gross = quantity * (sell_ask - buy_bid)
    both_fill_profit = both_fill_gross - buy_fee_maker - sell_fee_maker

    adverse_cost = capital_usd * (assumptions.adverse_move_pct / 100)
    one_leg_fee_taker = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital_usd, is_maker=False)
    one_leg_profit = -adverse_cost - one_leg_fee_taker

    p = assumptions.fill_probability
    p_both = p * p
    p_one = 2 * p * (1 - p)
    p_none = (1 - p) * (1 - p)

    expected_value = p_both * both_fill_profit + p_one * one_leg_profit  # p_none contributes 0

    return MakerScenarioResult(
        both_fill_profit_usd=both_fill_profit,
        one_leg_profit_usd=one_leg_profit,
        probability_both_fill=p_both,
        probability_one_leg_fills=p_one,
        probability_neither_fills=p_none,
        expected_value_usd=expected_value,
        expected_value_pct=expected_value / capital_usd * 100,
    )


def best_maker_pair(
    bids_asks: dict[str, tuple[float, float]],  # exchange -> (bid, ask)
    capital_usd: float,
    fee_engine: FeeEngine,
    assumptions: MakerAssumptions,
) -> tuple[str, str, MakerScenarioResult] | None:
    """Try every ordered exchange pair at one point in time, return the best expected value."""
    best: tuple[str, str, MakerScenarioResult] | None = None
    for buy_exchange, (buy_bid, _buy_ask) in bids_asks.items():
        for sell_exchange, (_sell_bid, sell_ask) in bids_asks.items():
            if buy_exchange == sell_exchange or buy_bid <= 0 or sell_ask <= 0:
                continue
            result = simulate_maker_leg_pair(
                buy_exchange, sell_exchange, buy_bid, sell_ask, capital_usd, fee_engine, assumptions
            )
            if best is None or result.expected_value_usd > best[2].expected_value_usd:
                best = (buy_exchange, sell_exchange, result)
    return best
