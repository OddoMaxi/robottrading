"""Engine A — Stablecoin Arbitrage (section 4).

Compares stablecoin pair prices across exchanges (USDT/USDC, USDT/FDUSD,
USDC/FDUSD) to find mispricings that should trade near 1:1.
"""

from app.analytics.fees import FeeEngine
from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, STABLECOIN_PAIRS, Strategy
from app.engines._shared import QuoteSpreadScanner
from app.engines.base import ArbitrageEngine
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.models import Opportunity


class StablecoinArbitrageEngine(ArbitrageEngine):
    strategy_name = Strategy.STABLECOIN

    def __init__(
        self,
        pairs: list[str] = STABLECOIN_PAIRS,
        store: MarketDataStore = market_data_store,
        fee_engine: FeeEngine = FeeEngine(),
        capital_usd: float = DEFAULT_OPPORTUNITY_CAPITAL_USD,
    ) -> None:
        self._scanner = QuoteSpreadScanner(Strategy.STABLECOIN, pairs, store, fee_engine, capital_usd)

    async def detect(self) -> list[Opportunity]:
        return await self._scanner.scan()
