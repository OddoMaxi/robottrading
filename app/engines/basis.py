"""Engine D — Spot / Futures Basis Arbitrage.

Compares spot price against a dated (quarterly) delivery future. Unlike
perpetual basis (Engine E — Funding), a dated future's basis MUST converge
to zero by its delivery date, purely by contract construction — no reliance
on the funding mechanism doing the work. That makes "annualized basis" a
meaningful yield figure here, the way it isn't for a perpetual.

LONG Spot + SHORT Future only (positive basis / contango) — shorting spot
outright isn't generally available on a retail spot account, so the reverse
trade (backwardation) isn't modeled.
"""

from app.analytics.fees import FeeEngine
from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, DELIVERY_FUTURES_ASSETS, MarketType, Strategy
from app.engines.base import ArbitrageEngine
from app.market_data.quality import DELIVERY_FUTURES_POLL_CADENCE_SECONDS, blocks_new_execution, build_feed_status
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.false_opportunity_filter import check_quote_freshness
from app.opportunity.models import Opportunity

DAYS_PER_YEAR = 365.0
MIN_DAYS_TO_EXPIRY = 1.0  # floor to avoid an exploding annualized figure right before settlement


class BasisArbitrageEngine(ArbitrageEngine):
    strategy_name = Strategy.BASIS

    def __init__(
        self,
        assets: list[str] = DELIVERY_FUTURES_ASSETS,
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
            for exchange, future in self.store.delivery_futures_for_symbol(symbol).items():
                spot = spot_quotes.get(exchange)
                if spot is None or spot.ask <= 0 or future.price <= 0:
                    continue

                spot_freshness = check_quote_freshness(spot, future.received_at)
                if not spot_freshness.is_valid:
                    continue

                # Market Data Quality Engine (Reality Engine spec, section 5)
                # — the delivery-futures snapshot itself was never checked
                # for staleness before this: if the REST poller died but
                # spot kept ticking, this engine would happily keep pricing
                # basis off an arbitrarily old futures price.
                future_status = build_feed_status(
                    exchange, symbol, "delivery_futures", future.received_at, DELIVERY_FUTURES_POLL_CADENCE_SECONDS
                )
                if blocks_new_execution(future_status.health):
                    continue

                days_to_expiry = max(MIN_DAYS_TO_EXPIRY, (future.delivery_time - future.received_at) / 86400)
                basis_pct = (future.price - spot.ask) / spot.ask * 100
                if basis_pct <= 0:
                    continue  # contango only — see module docstring

                annualized_pct = basis_pct * (DAYS_PER_YEAR / days_to_expiry)

                quantity = self.capital_usd / spot.ask
                spot_fee = self.fee_engine.trading_fee(exchange, MarketType.SPOT, self.capital_usd, is_maker=False)
                future_notional = quantity * future.price
                future_fee = self.fee_engine.trading_fee(exchange, MarketType.FUTURES, future_notional, is_maker=False)

                # Held to expiry, the future settles to the spot price by
                # construction — the basis captured at entry *is* the profit,
                # no assumption needed about early convergence.
                gross_profit = quantity * (future.price - spot.ask)
                net_profit = gross_profit - spot_fee - future_fee
                net_spread_pct = net_profit / self.capital_usd * 100

                opportunities.append(
                    Opportunity(
                        strategy=Strategy.BASIS,
                        symbol=symbol,
                        legs=[
                            {"exchange": exchange, "side": "buy", "market": "spot", "price": spot.ask, "quantity": quantity},
                            {
                                "exchange": exchange,
                                "side": "sell",
                                "market": "futures",
                                "symbol": future.contract_symbol,
                                "price": future.price,
                                "quantity": quantity,
                            },
                        ],
                        gross_spread_pct=basis_pct,
                        net_spread_pct=net_spread_pct,
                        capital_usd=self.capital_usd,
                        expected_profit_usd=net_profit,
                        annualized_pct=annualized_pct,
                        days_to_expiry=days_to_expiry,
                        market_data_age_seconds=spot_freshness.market_data_age_seconds,
                        holding_period_seconds=days_to_expiry * 86400,
                        capital_is_liquidity_capped=False,  # no depth data for the futures leg
                    )
                )
        return opportunities
