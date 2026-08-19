"""Paper Trading Engine (section 19) — replays a priced Opportunity against a
portfolio, with a realistic execution outcome rather than assuming every
trade fills perfectly.

The execution outcome (SIMULATED_EXECUTED / PARTIAL_FILL / MISSED /
SIMULATED_FAILED) is a property of the opportunity itself — determined once
per opportunity, not once per (opportunity, portfolio) pair, since a maker
order either fills or it doesn't regardless of which virtual portfolio is
tracking the result.
"""

import random
from dataclasses import dataclass
from enum import StrEnum

from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD
from app.opportunity.false_opportunity_filter import MAX_QUOTE_AGE_SECONDS
from app.opportunity.models import Opportunity
from app.simulation.portfolios import VirtualPortfolio

# capital_usd coming in below this fraction of the standard order size means
# the Liquidity Engine's VWAP simulation already capped the fillable size —
# a genuine partial fill, not a fully-sized trade.
PARTIAL_FILL_THRESHOLD = 0.9


class TradeStatus(StrEnum):
    SIMULATED_EXECUTED = "simulated_executed"
    PARTIAL_FILL = "partial_fill"
    MISSED = "missed"  # a maker leg didn't fill in time — costs nothing, see app.execution.maker_taker
    SIMULATED_FAILED = "simulated_failed"  # market data was already stale when priced


@dataclass(slots=True)
class SimulatedTrade:
    opportunity_id: str
    portfolio_name: str
    status: TradeStatus
    capital_usd: float
    gross_profit_usd: float
    fees_usd: float
    net_profit_usd: float


class PaperTrader:
    def __init__(self, rng: random.Random | None = None) -> None:
        self._rng = rng or random.Random()

    def determine_outcome(self, opportunity: Opportunity) -> TradeStatus:
        """Sample the execution outcome once per opportunity — shared across every portfolio's replay of it."""
        if opportunity.market_data_age_seconds is not None and opportunity.market_data_age_seconds > MAX_QUOTE_AGE_SECONDS:
            return TradeStatus.SIMULATED_FAILED

        if (
            opportunity.execution_mode
            and opportunity.execution_mode != "taker_taker"
            and opportunity.execution_fill_probability is not None
            and self._rng.random() > opportunity.execution_fill_probability
        ):
            return TradeStatus.MISSED

        if opportunity.capital_usd is not None and opportunity.capital_usd < DEFAULT_OPPORTUNITY_CAPITAL_USD * PARTIAL_FILL_THRESHOLD:
            return TradeStatus.PARTIAL_FILL

        return TradeStatus.SIMULATED_EXECUTED

    def simulate(self, opportunity: Opportunity, portfolio: VirtualPortfolio, status: TradeStatus) -> SimulatedTrade:
        if not opportunity.capital_usd or opportunity.expected_profit_usd is None:
            raise ValueError("Opportunity must be fully priced before paper trading")

        if status in (TradeStatus.MISSED, TradeStatus.SIMULATED_FAILED):
            return SimulatedTrade(str(opportunity.id), portfolio.name, status, 0.0, 0.0, 0.0, 0.0)

        capital = min(opportunity.capital_usd, portfolio.current_value_usd)
        scale = capital / opportunity.capital_usd
        net_profit = opportunity.expected_profit_usd * scale
        gross_profit = capital * (opportunity.gross_spread_pct / 100)
        fees = gross_profit - net_profit

        portfolio.balances["USDT"] = portfolio.balances.get("USDT", 0.0) + net_profit

        return SimulatedTrade(str(opportunity.id), portfolio.name, status, capital, gross_profit, fees, net_profit)
