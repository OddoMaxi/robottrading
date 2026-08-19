"""Engine C — Triangular Arbitrage (section 6).

Walks fixed conversion loops within a single exchange (base -> leg1 -> leg2
-> base, e.g. USDT -> BTC -> ETH -> USDT), capped at each leg by the
available top-of-book depth, net of all three legs' taker fees.
"""

from app.analytics.fees import FeeEngine
from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, TRIANGULAR_PATHS, MarketType, Strategy
from app.engines.base import ArbitrageEngine
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.models import Opportunity


class TriangularArbitrageEngine(ArbitrageEngine):
    strategy_name = Strategy.TRIANGULAR

    def __init__(
        self,
        exchange: str,
        paths: list[tuple[str, str, str]] = TRIANGULAR_PATHS,
        store: MarketDataStore = market_data_store,
        fee_engine: FeeEngine = FeeEngine(),
        capital_usd: float = DEFAULT_OPPORTUNITY_CAPITAL_USD,
    ) -> None:
        self.exchange = exchange
        self.paths = paths
        self.store = store
        self.fee_engine = fee_engine
        self.capital_usd = capital_usd

    async def detect(self) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for base, leg1_asset, leg2_asset in self.paths:
            opp = self._evaluate_path(base, leg1_asset, leg2_asset)
            if opp is not None:
                opportunities.append(opp)
        return opportunities

    def _evaluate_path(self, base: str, leg1_asset: str, leg2_asset: str) -> Opportunity | None:
        sym1 = f"{leg1_asset}/{base}"  # e.g. BTC/USDT
        sym2 = f"{leg2_asset}/{leg1_asset}"  # e.g. ETH/BTC
        sym3 = f"{leg2_asset}/{base}"  # e.g. ETH/USDT

        q1 = self.store.get_quote(self.exchange, MarketType.SPOT, sym1)
        q2 = self.store.get_quote(self.exchange, MarketType.SPOT, sym2)
        q3 = self.store.get_quote(self.exchange, MarketType.SPOT, sym3)
        if not (q1 and q2 and q3) or q1.ask <= 0 or q2.ask <= 0 or q3.bid <= 0:
            return None

        # Leg 1: BUY leg1_asset with base, capped by top-of-book ask depth.
        leg1_spend = min(self.capital_usd, q1.ask * q1.ask_quantity)
        leg1_qty = leg1_spend / q1.ask

        # Leg 2: BUY leg2_asset with leg1_asset, capped by top-of-book ask depth.
        leg2_spend = min(leg1_qty, q2.ask * q2.ask_quantity)
        leg2_qty = leg2_spend / q2.ask

        # Leg 3: SELL leg2_asset for base, capped by top-of-book bid depth.
        leg3_qty = min(leg2_qty, q3.bid_quantity)
        end_capital = leg3_qty * q3.bid

        if leg1_spend <= 0 or leg2_spend <= 0 or leg3_qty <= 0:
            return None

        gross_spread_pct = (end_capital - leg1_spend) / leg1_spend * 100
        if gross_spread_pct <= 0:
            return None

        fee1 = self.fee_engine.trading_fee(self.exchange, MarketType.SPOT, leg1_spend, is_maker=False)
        fee2 = self.fee_engine.trading_fee(self.exchange, MarketType.SPOT, leg2_spend, is_maker=False)
        fee3 = self.fee_engine.trading_fee(self.exchange, MarketType.SPOT, end_capital, is_maker=False)
        net_profit = end_capital - leg1_spend - fee1 - fee2 - fee3
        net_spread_pct = net_profit / leg1_spend * 100

        return Opportunity(
            strategy=Strategy.TRIANGULAR,
            symbol=f"{base}->{leg1_asset}->{leg2_asset}->{base}",
            legs=[
                {"exchange": self.exchange, "side": "buy", "market": "spot", "symbol": sym1, "price": q1.ask, "quantity": leg1_qty},
                {"exchange": self.exchange, "side": "buy", "market": "spot", "symbol": sym2, "price": q2.ask, "quantity": leg2_qty},
                {"exchange": self.exchange, "side": "sell", "market": "spot", "symbol": sym3, "price": q3.bid, "quantity": leg3_qty},
            ],
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            capital_usd=leg1_spend,
            expected_profit_usd=net_profit,
        )
