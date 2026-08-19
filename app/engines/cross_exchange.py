"""Engine B — Cross-Exchange Arbitrage (section 5).

Compares the same asset's best bid/ask across exchanges (e.g. BUY on the
exchange with the lowest ask, SELL on the exchange with the highest bid).
"""

from app.analytics.fees import FeeEngine
from app.config.constants import CROSS_EXCHANGE_ASSETS, DEFAULT_OPPORTUNITY_CAPITAL_USD, Strategy
from app.engines._shared import QuoteSpreadScanner
from app.engines.base import ArbitrageEngine
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.models import Opportunity


class CrossExchangeArbitrageEngine(ArbitrageEngine):
    strategy_name = Strategy.CROSS_EXCHANGE

    def __init__(
        self,
        assets: list[str] = CROSS_EXCHANGE_ASSETS,
        quote_asset: str = "USDT",
        store: MarketDataStore = market_data_store,
        fee_engine: FeeEngine = FeeEngine(),
        capital_usd: float = DEFAULT_OPPORTUNITY_CAPITAL_USD,
    ) -> None:
        symbols = [f"{asset}/{quote_asset}" for asset in assets]
        self._scanner = QuoteSpreadScanner(Strategy.CROSS_EXCHANGE, symbols, store, fee_engine, capital_usd)

    async def detect(self) -> list[Opportunity]:
        return await self._scanner.scan()
