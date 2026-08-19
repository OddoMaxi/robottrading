"""Paper Trading Engine (section 19) — replays a priced Opportunity against a portfolio.

Never sends real orders. The opportunity is already fully priced by its
engine (gross/net spread, capital, expected net profit) — this just scales
that result to the capital actually available in the portfolio and books it.
"""

from dataclasses import dataclass

from app.opportunity.models import Opportunity
from app.simulation.portfolios import VirtualPortfolio


@dataclass(slots=True)
class SimulatedTrade:
    opportunity_id: str
    portfolio_name: str
    capital_usd: float
    gross_profit_usd: float
    fees_usd: float
    net_profit_usd: float


class PaperTrader:
    def simulate(self, opportunity: Opportunity, portfolio: VirtualPortfolio) -> SimulatedTrade:
        if not opportunity.capital_usd or opportunity.expected_profit_usd is None:
            raise ValueError("Opportunity must be fully priced before paper trading")

        capital = min(opportunity.capital_usd, portfolio.current_value_usd)
        scale = capital / opportunity.capital_usd
        net_profit = opportunity.expected_profit_usd * scale
        gross_profit = capital * (opportunity.gross_spread_pct / 100)
        fees = gross_profit - net_profit

        portfolio.balances["USDT"] = portfolio.balances.get("USDT", 0.0) + net_profit

        return SimulatedTrade(
            opportunity_id=str(opportunity.id),
            portfolio_name=portfolio.name,
            capital_usd=capital,
            gross_profit_usd=gross_profit,
            fees_usd=fees,
            net_profit_usd=net_profit,
        )
