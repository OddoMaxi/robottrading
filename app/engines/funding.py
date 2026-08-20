"""Engine D — Funding / Spot-Futures Arbitrage (section 7).

LONG Spot + SHORT Perpetual: captures the funding rate plus any spot/perp
basis, net of both legs' taker fees.
"""

import time

from app.analytics.fees import FeeEngine
from app.config.constants import CROSS_EXCHANGE_ASSETS, DEFAULT_OPPORTUNITY_CAPITAL_USD, MarketType, Strategy
from app.engines.base import ArbitrageEngine
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.false_opportunity_filter import check_quote_freshness
from app.opportunity.models import Opportunity

# Funding is paid repeatedly (every 8h on Binance/Bybit, similarly on OKX) for
# as long as the carry position is held — comparing one funding payment
# against the full one-time entry cost of both legs understates the trade by
# construction. 9 periods ≈ 3 days is a conservative minimum realistic hold.
HOLDING_PERIOD_FUNDING_EVENTS = 9
FUNDING_INTERVAL_SECONDS = 8 * 3600


class FundingArbitrageEngine(ArbitrageEngine):
    strategy_name = Strategy.FUNDING

    def __init__(
        self,
        assets: list[str] = CROSS_EXCHANGE_ASSETS,
        quote_asset: str = "USDT",
        store: MarketDataStore = market_data_store,
        fee_engine: FeeEngine = FeeEngine(),
        capital_usd: float = DEFAULT_OPPORTUNITY_CAPITAL_USD,
        holding_period_funding_events: int = HOLDING_PERIOD_FUNDING_EVENTS,
    ) -> None:
        self.assets = assets
        self.quote_asset = quote_asset
        self.store = store
        self.fee_engine = fee_engine
        self.capital_usd = capital_usd
        self.holding_period_funding_events = holding_period_funding_events

    async def detect(self) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for asset in self.assets:
            symbol = f"{asset}/{self.quote_asset}"
            spot_quotes = self.store.quotes_for_symbol(MarketType.SPOT, symbol)
            for exchange, funding in self.store.funding_for_symbol(symbol).items():
                spot = spot_quotes.get(exchange)
                if spot is None or spot.ask <= 0 or funding.mark_price <= 0:
                    continue

                # False Opportunity Filter: only the spot leg is tick-driven
                # (funding snapshots are REST-polled every ~30s by design —
                # applying the same tick-freshness bar to them would reject
                # perfectly valid funding data).
                spot_freshness = check_quote_freshness(spot, time.time())
                if not spot_freshness.is_valid:
                    continue

                basis_pct = (funding.mark_price - spot.ask) / spot.ask * 100
                expected_funding_pct = funding.funding_rate * 100
                gross_spread_pct = expected_funding_pct + basis_pct
                if gross_spread_pct <= 0:
                    continue

                quantity = self.capital_usd / spot.ask
                perp_notional = quantity * funding.mark_price

                # Round-trip (open + close) taker fees on both legs — the real
                # one-time cost of the carry trade, paid once regardless of
                # how long the position is held.
                spot_fee_round_trip = 2 * self.fee_engine.trading_fee(
                    exchange, MarketType.SPOT, self.capital_usd, is_maker=False
                )
                perp_fee_round_trip = 2 * self.fee_engine.trading_fee(
                    exchange, MarketType.PERPETUAL, perp_notional, is_maker=False
                )

                # Funding income compounds over the holding period; basis is
                # deliberately not counted here (no honest way to assume it
                # converges in our favor from a single reading) — conservative.
                total_funding_income = perp_notional * funding.funding_rate * self.holding_period_funding_events
                net_profit = total_funding_income - spot_fee_round_trip - perp_fee_round_trip
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
                        market_data_age_seconds=spot_freshness.market_data_age_seconds,
                        holding_period_seconds=self.holding_period_funding_events * FUNDING_INTERVAL_SECONDS,
                        capital_is_liquidity_capped=False,  # no depth data for the perpetual leg
                    )
                )
        return opportunities
