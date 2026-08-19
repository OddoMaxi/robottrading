"""Shared spread-scanning logic for engines that compare one symbol's best
bid/ask across exchanges — Cross-Exchange (section 5) and Stablecoin
(section 4) arbitrage are the same mechanic over a different symbol universe.
"""

from app.analytics.fees import FeeEngine
from app.config.constants import MarketType, Strategy
from app.market_data.orderbook import OrderBookLevel, simulate_vwap
from app.market_data.store import MarketDataStore
from app.opportunity.models import Opportunity


class QuoteSpreadScanner:
    def __init__(
        self,
        strategy: Strategy,
        symbols: list[str],
        store: MarketDataStore,
        fee_engine: FeeEngine,
        capital_usd: float,
    ) -> None:
        self.strategy = strategy
        self.symbols = symbols
        self.store = store
        self.fee_engine = fee_engine
        self.capital_usd = capital_usd

    async def scan(self) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for symbol in self.symbols:
            quotes = self.store.quotes_for_symbol(MarketType.SPOT, symbol)
            for buy_exchange, buy_quote in quotes.items():
                for sell_exchange, sell_quote in quotes.items():
                    if buy_exchange == sell_exchange or buy_quote.ask <= 0 or sell_quote.bid <= 0:
                        continue
                    gross_spread_pct = (sell_quote.bid - buy_quote.ask) / buy_quote.ask * 100
                    if gross_spread_pct <= 0:
                        continue
                    opp = self._price(symbol, buy_exchange, buy_quote, sell_exchange, sell_quote, gross_spread_pct)
                    if opp is not None:
                        opportunities.append(opp)
        return opportunities

    def _price(self, symbol, buy_exchange, buy_quote, sell_exchange, sell_quote, gross_spread_pct) -> Opportunity | None:
        # V1 collectors only carry top-of-book (bookTicker/tickers streams), so
        # the "order book" fed to the Liquidity/Slippage engines has one level.
        buy_fill = simulate_vwap([OrderBookLevel(buy_quote.ask, buy_quote.ask_quantity)], self.capital_usd)
        sell_fill = simulate_vwap([OrderBookLevel(sell_quote.bid, sell_quote.bid_quantity)], self.capital_usd)
        if buy_fill.filled_usd <= 0 or sell_fill.filled_usd <= 0:
            return None

        capital = min(buy_fill.filled_usd, sell_fill.filled_usd)
        quantity = capital / buy_fill.average_price

        buy_fee = self.fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital, is_maker=False)
        sell_notional = quantity * sell_fill.average_price
        sell_fee = self.fee_engine.trading_fee(sell_exchange, MarketType.SPOT, sell_notional, is_maker=False)

        gross_profit = quantity * (sell_fill.average_price - buy_fill.average_price)
        net_profit = gross_profit - buy_fee - sell_fee
        net_spread_pct = net_profit / capital * 100

        return Opportunity(
            strategy=self.strategy,
            symbol=symbol,
            legs=[
                {"exchange": buy_exchange, "side": "buy", "market": "spot", "price": buy_fill.average_price, "quantity": quantity},
                {"exchange": sell_exchange, "side": "sell", "market": "spot", "price": sell_fill.average_price, "quantity": quantity},
            ],
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            capital_usd=capital,
            expected_profit_usd=net_profit,
        )
