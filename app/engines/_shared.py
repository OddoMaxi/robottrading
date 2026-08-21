"""Shared spread-scanning logic for engines that compare one symbol's best
bid/ask across exchanges — Cross-Exchange (section 5) and Stablecoin
(section 4) arbitrage are the same mechanic over a different symbol universe.
"""

import time

from app.analytics.break_even import compute_break_even
from app.analytics.fees import FeeEngine
from app.config.constants import NOMINAL_FAST_HOLDING_SECONDS, MarketType, Strategy
from app.execution.execution_simulator import simulate_best_execution
from app.market_data.normalizer import NormalizedQuote
from app.market_data.orderbook import OrderBookLevel, simulate_vwap
from app.market_data.store import MarketDataStore
from app.opportunity.false_opportunity_filter import check_leg_pair_sync
from app.opportunity.models import Opportunity

# Reality Engine spec, sections 7-8 — a real order book snapshot older than
# this is treated the same as not having one (falls back to the top-of-book
# approximation), rather than VWAP-pricing a trade against depth that's
# stopped reflecting the live book. depth20@100ms updates every 100ms, so
# anything past a couple of seconds means that feed has stalled.
MAX_ORDER_BOOK_AGE_SECONDS = 2.0


def _resolve_ask_levels(store: MarketDataStore, exchange: str, symbol: str, quote: NormalizedQuote, now: float) -> list[OrderBookLevel]:
    """Real multi-level depth where a fresh local order book exists for
    this (exchange, symbol) — Binance spot only, for now (section 53) —
    else the same single top-of-book level engines have always used."""
    book = store.get_order_book(exchange, symbol)
    if book is not None and book.asks and (now - book.timestamp) <= MAX_ORDER_BOOK_AGE_SECONDS:
        return book.asks
    return [OrderBookLevel(quote.ask, quote.ask_quantity)]


def _resolve_bid_levels(store: MarketDataStore, exchange: str, symbol: str, quote: NormalizedQuote, now: float) -> list[OrderBookLevel]:
    book = store.get_order_book(exchange, symbol)
    if book is not None and book.bids and (now - book.timestamp) <= MAX_ORDER_BOOK_AGE_SECONDS:
        return book.bids
    return [OrderBookLevel(quote.bid, quote.bid_quantity)]


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

                    # False Opportunity Filter: reject stale or desynced
                    # quotes before treating the gap between them as real.
                    data_quality = check_leg_pair_sync(buy_quote, sell_quote, time.time())
                    if not data_quality.is_valid:
                        continue

                    gross_spread_pct = (sell_quote.bid - buy_quote.ask) / buy_quote.ask * 100
                    if gross_spread_pct <= 0:
                        continue

                    # Break-Even Engine: cheap upfront gate, fees alone are
                    # known without touching the order book. Gate on the
                    # MAKER/MAKER break-even — the lowest of the 4 execution
                    # modes' thresholds — so an opportunity that could only
                    # ever be profitable via maker orders still gets through
                    # to the Maker/Taker Strategy Engine below. The taker
                    # break-even (the conservative, certain-fill reference)
                    # is still what gets reported and used for classification.
                    taker_break_even = compute_break_even(
                        self.fee_engine, [(buy_exchange, MarketType.SPOT, False), (sell_exchange, MarketType.SPOT, False)]
                    )
                    maker_break_even = compute_break_even(
                        self.fee_engine, [(buy_exchange, MarketType.SPOT, True), (sell_exchange, MarketType.SPOT, True)]
                    )
                    if gross_spread_pct < maker_break_even.total_pct:
                        continue

                    opp = self._price(
                        symbol,
                        buy_exchange,
                        buy_quote,
                        sell_exchange,
                        sell_quote,
                        gross_spread_pct,
                        taker_break_even.total_pct,
                        data_quality.market_data_age_seconds,
                    )
                    if opp is not None:
                        opportunities.append(opp)
        return opportunities

    def _price(
        self, symbol, buy_exchange, buy_quote, sell_exchange, sell_quote, gross_spread_pct, break_even_pct, market_data_age_seconds
    ) -> Opportunity | None:
        # Real multi-level depth where a local order book is available and
        # fresh (Binance spot, section 53's "Core Exchange" priority) —
        # everywhere else, still the single top-of-book level collectors
        # have always carried (bookTicker/tickers streams).
        now = time.time()
        buy_fill = simulate_vwap(_resolve_ask_levels(self.store, buy_exchange, symbol, buy_quote, now), self.capital_usd)
        sell_fill = simulate_vwap(_resolve_bid_levels(self.store, sell_exchange, symbol, sell_quote, now), self.capital_usd)
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

        # Maker/Taker Strategy Engine: on top of the certain-fill taker/taker
        # numbers above, work out whether resting a maker order on either
        # leg beats it in expectation, given the fill risk.
        best_execution = simulate_best_execution(buy_exchange, sell_exchange, buy_quote, sell_quote, capital, self.fee_engine, self.store)

        return Opportunity(
            strategy=self.strategy,
            symbol=symbol,
            legs=[
                {"exchange": buy_exchange, "side": "buy", "market": "spot", "price": buy_fill.average_price, "quantity": quantity},
                {"exchange": sell_exchange, "side": "sell", "market": "spot", "price": sell_fill.average_price, "quantity": quantity},
            ],
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            break_even_pct=break_even_pct,
            capital_usd=capital,
            expected_profit_usd=net_profit,
            execution_mode=best_execution.mode,
            execution_fill_probability=best_execution.fill_probability,
            market_data_age_seconds=market_data_age_seconds,
            holding_period_seconds=NOMINAL_FAST_HOLDING_SECONDS,
        )
