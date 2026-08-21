"""Engine C — Triangular Arbitrage (section 6).

Walks a 3-asset conversion loop within a single exchange — e.g. USDT ->
BTC -> ETH -> USDT, or a stablecoin-bridge loop like USDC -> ETH -> USDT
-> USDC — capped at each hop by the available top-of-book depth, net of
all three legs' taker fees.

Each hop can be a BUY or a SELL depending on which direction is actually
listed: real markets only ever list a crypto asset as the base against a
stablecoin (or BTC) quote, never the reverse. The engine discovers the
right direction per hop instead of assuming a fixed buy/buy/sell shape —
that's what lets a stablecoin-bridge loop (which resolves to buy, sell,
buy — e.g. USDC -> ETH is a buy, ETH -> USDT is a sell, USDT -> USDC is a
buy) run through the same code path as the original crypto-bridge loops
(buy, buy, sell).
"""

import time
from dataclasses import dataclass

from app.analytics.break_even import compute_break_even
from app.analytics.fees import FeeEngine
from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, NOMINAL_FAST_HOLDING_SECONDS, TRIANGULAR_PATHS, MarketType, Strategy
from app.engines.base import ArbitrageEngine
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore, market_data_store
from app.opportunity.false_opportunity_filter import check_quote_freshness
from app.opportunity.models import Opportunity


@dataclass(slots=True)
class _Hop:
    symbol: str
    side: str  # "buy" or "sell"
    quote: NormalizedQuote


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
        # All 3 legs are on the same exchange (taker) — fixed per instance,
        # so the break-even floor is computed once rather than per scan.
        three_taker_legs = [(exchange, MarketType.SPOT, False)] * 3
        self.break_even_pct = compute_break_even(fee_engine, three_taker_legs).total_pct

    async def detect(self) -> list[Opportunity]:
        # Opportunity Expansion spec, Step 3 (user directive, 2026-08-21) —
        # each configured (base, leg1, leg2) triple is one loop shape;
        # walking it forward (base->leg1->leg2->base) and backward
        # (base->leg2->leg1->base) are two economically distinct paths that
        # can each independently clear or miss break-even, not the same
        # opportunity seen twice — the forward direction buys leg1 and sells
        # leg2 against it, the reverse does the opposite, and real order
        # books are almost never symmetric enough for both to be identical.
        opportunities: list[Opportunity] = []
        for base, leg1_asset, leg2_asset in self.paths:
            for a, b in ((leg1_asset, leg2_asset), (leg2_asset, leg1_asset)):
                opp = self._evaluate_path(base, a, b)
                if opp is not None:
                    opportunities.append(opp)
        return opportunities

    def _find_hop(self, from_asset: str, to_asset: str) -> _Hop | None:
        """Whichever direction is actually listed: buy `to_asset` with
        `from_asset` if <to>/<from> exists, else sell `from_asset` for
        `to_asset` if <from>/<to> exists."""
        buy_symbol = f"{to_asset}/{from_asset}"
        q = self.store.get_quote(self.exchange, MarketType.SPOT, buy_symbol)
        if q and q.ask > 0:
            return _Hop(buy_symbol, "buy", q)
        sell_symbol = f"{from_asset}/{to_asset}"
        q = self.store.get_quote(self.exchange, MarketType.SPOT, sell_symbol)
        if q and q.bid > 0:
            return _Hop(sell_symbol, "sell", q)
        return None

    def _evaluate_path(self, base: str, leg1_asset: str, leg2_asset: str) -> Opportunity | None:
        loop = [base, leg1_asset, leg2_asset, base]
        hops = [self._find_hop(loop[i], loop[i + 1]) for i in range(3)]
        if any(h is None for h in hops):
            return None

        # False Opportunity Filter: all 3 legs must be reasonably fresh —
        # a triangle priced off a stale rate on one hop isn't real.
        now = time.time()
        checks = [check_quote_freshness(h.quote, now) for h in hops]
        if any(not c.is_valid for c in checks):
            return None
        market_data_age_seconds = max(c.market_data_age_seconds for c in checks)

        # Walk the loop once, tracking the running quantity in whatever
        # asset we currently hold and that asset's own USD price (so each
        # hop's fee — always a % of USD notional — is computed correctly
        # even for a middle hop priced in a non-stablecoin, e.g. ETH/BTC).
        quantity = self.capital_usd  # units of `base`, a stablecoin ≈ $1/unit
        asset_usd_price = 1.0
        legs = []
        fee_notionals_usd = []

        for hop in hops:
            q = hop.quote
            if hop.side == "buy":
                # Market is to_asset/from_asset: spend `quantity` units of
                # from_asset, capped by ask depth (in from_asset terms as
                # price * ask_quantity); receive to_asset.
                spend = min(quantity, q.ask * q.ask_quantity)
                if spend <= 0:
                    return None
                received = spend / q.ask
                legs.append({"exchange": self.exchange, "side": "buy", "market": "spot", "symbol": hop.symbol, "price": q.ask, "quantity": received})
                fee_notionals_usd.append(spend * asset_usd_price)
                asset_usd_price *= q.ask
            else:  # sell
                # Market is from_asset/to_asset: sell `quantity` units of
                # from_asset, capped by bid depth (in from_asset units);
                # receive to_asset.
                sell_qty = min(quantity, q.bid_quantity)
                if sell_qty <= 0:
                    return None
                received = sell_qty * q.bid
                legs.append({"exchange": self.exchange, "side": "sell", "market": "spot", "symbol": hop.symbol, "price": q.bid, "quantity": sell_qty})
                fee_notionals_usd.append(sell_qty * asset_usd_price)
                asset_usd_price /= q.bid
            quantity = received

        end_capital = quantity  # back in `base` units, a stablecoin ≈ $1/unit
        leg1_spend = fee_notionals_usd[0]  # asset_usd_price was still 1.0 at hop 0

        gross_spread_pct = (end_capital - leg1_spend) / leg1_spend * 100
        if gross_spread_pct <= 0:
            return None

        # Break-Even Engine: skip the fee math below entirely if the gross
        # spread can't even clear the 3-leg fee floor + standard buffers.
        if gross_spread_pct < self.break_even_pct:
            return None

        fees_usd = [self.fee_engine.trading_fee(self.exchange, MarketType.SPOT, n, is_maker=False) for n in fee_notionals_usd]
        net_profit = end_capital - leg1_spend - sum(fees_usd)
        net_spread_pct = net_profit / leg1_spend * 100

        return Opportunity(
            strategy=Strategy.TRIANGULAR,
            symbol=f"{base}->{leg1_asset}->{leg2_asset}->{base}",
            legs=legs,
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            break_even_pct=self.break_even_pct,
            capital_usd=leg1_spend,
            expected_profit_usd=net_profit,
            market_data_age_seconds=market_data_age_seconds,
            holding_period_seconds=NOMINAL_FAST_HOLDING_SECONDS,
        )
