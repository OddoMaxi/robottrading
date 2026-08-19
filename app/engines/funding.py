"""Engine D — Funding / Spot-Futures Arbitrage (section 7).

LONG Spot + SHORT Perpetual: captures the funding rate plus any spot/perp
basis, net of both legs' taker fees.
"""

from app.analytics.fees import FeeEngine
from app.config.constants import CROSS_EXCHANGE_ASSETS, DEFAULT_OPPORTUNITY_CAPITAL_USD, MarketType, Strategy
from app.engines.base import ArbitrageEngine
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.models import Opportunity


class FundingArbitrageEngine(ArbitrageEngine):
    strategy_name = Strategy.FUNDING

    def __init__(
        self,
        assets: list[str] = CROSS_EXCHANGE_ASSETS,
        quote_asset: str = "USDT",
        store: MarketDataStore = market_data_store,
        fee_engine: FeeEngine = FeeEngine(),
        capital_usd: float = DEFAULT_OPPORTUNITY_CAPITAL_USD,
    ) -> None:
        self.assets = assets
        self.quote_asset = quote_asset
        self.store = store
        self.fee_engine = fee_engine
        self.capital_usd = capital_usd

    async def detect(self) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for asset in self.assets:
            symbol = f"{asset}/{self.quote_asset}"
            spot_quotes = self.store.quotes_for_symbol(MarketType.SPOT, symbol)
            for exchange, funding in self.store.funding_for_symbol(symbol).items():
                spot = spot_quotes.get(exchange)
                if spot is None or spot.ask <= 0 or funding.mark_price <= 0:
                    continue

                basis_pct = (funding.mark_price - spot.ask) / spot.ask * 100
                expected_funding_pct = funding.funding_rate * 100
                gross_spread_pct = expected_funding_pct + basis_pct
                if gross_spread_pct <= 0:
                    continue

                quantity = self.capital_usd / spot.ask
                spot_fee = self.fee_engine.trading_fee(exchange, MarketType.SPOT, self.capital_usd, is_maker=False)
                perp_notional = quantity * funding.mark_price
                perp_fee = self.fee_engine.trading_fee(exchange, MarketType.PERPETUAL, perp_notional, is_maker=False)
                funding_income = perp_notional * funding.funding_rate
                net_profit = funding_income - spot_fee - perp_fee
                net_spread_pct = net_profit / self.capital_usd * 100

                opportunities.append(
                    Opportunity(
                        strategy=Strategy.FUNDING,
                        symbol=symbol,
                        legs=[
                            {"exchange": exchange, "side": "buy", "market": "spot", "price": spot.ask, "quantity": quantity},
                            {"exchange": exchange, "side": "sell", "market": "perpetual", "price": funding.mark_price, "quantity": quantity},
                        ],
                        gross_spread_pct=gross_spread_pct,
                        net_spread_pct=net_spread_pct,
                        capital_usd=self.capital_usd,
                        expected_profit_usd=net_profit,
                    )
                )
        return opportunities
