"""Shared in-memory market data store (V1: single process).

Collectors write into it, engines read from it. A plain dict is safe here
because asyncio is single-threaded and cooperative — each update is one
atomic dict assignment, never interleaved with a read.
"""

import asyncio
import statistics
from collections import deque
from dataclasses import dataclass

from app.config.constants import MarketType
from app.market_data.normalizer import NormalizedQuote
from app.market_data.orderbook import OrderBook

# Samples kept per (exchange, symbol) for a lightweight, no-DB-roundtrip
# volatility estimate — feeds the Maker Fill Probability heuristic
# (app/execution/fill_probability.py) without slowing down the hot
# detection path with a database query.
VOLATILITY_WINDOW_SIZE = 20


@dataclass(slots=True)
class FundingSnapshot:
    exchange: str
    symbol: str  # common form, e.g. "BTC/USDT" — the perpetual on that asset/quote
    funding_rate: float
    next_funding_time: float  # epoch seconds
    mark_price: float
    index_price: float
    received_at: float


@dataclass(slots=True)
class DeliveryFuturesSnapshot:
    exchange: str
    symbol: str  # common form, e.g. "BTC/USDT" — the underlying asset/quote
    contract_symbol: str  # exchange-native dated contract symbol, e.g. "BTCUSDT_250926"
    price: float
    delivery_time: float  # epoch seconds
    received_at: float


class MarketDataStore:
    def __init__(self) -> None:
        self._quotes: dict[tuple[str, MarketType, str], NormalizedQuote] = {}
        self._funding: dict[tuple[str, str], FundingSnapshot] = {}
        # Lets the detection loop react within milliseconds of a new tick
        # instead of waiting up to a fixed poll interval — a real spread
        # observed on 2026-08-19 opened and closed within ~15 seconds, which
        # a 3s poll could miss most of.
        self._update_event = asyncio.Event()
        self._mid_price_history: dict[tuple[str, str], deque[float]] = {}
        self._delivery_futures: dict[tuple[str, str], DeliveryFuturesSnapshot] = {}
        # Reality Engine spec, sections 7-8 — real multi-level depth,
        # separate from the top-of-book NormalizedQuote store above. Keyed
        # (exchange, symbol); populated only where a depth collector is
        # actually wired up (Binance spot first — section 53's "Binance =
        # Core Exchange"), so engines must treat this as optional and fall
        # back to the top-of-book approximation where it's absent.
        self._order_books: dict[tuple[str, str], OrderBook] = {}

    def update_order_book(self, book: OrderBook) -> None:
        self._order_books[(book.exchange, book.symbol)] = book
        self._update_event.set()

    def get_order_book(self, exchange: str, symbol: str) -> OrderBook | None:
        return self._order_books.get((exchange, symbol))

    def update_quote(self, quote: NormalizedQuote) -> None:
        self._quotes[(quote.exchange, quote.market, quote.symbol)] = quote
        history_key = (quote.exchange, quote.symbol)
        history = self._mid_price_history.setdefault(history_key, deque(maxlen=VOLATILITY_WINDOW_SIZE))
        history.append((quote.bid + quote.ask) / 2)
        self._update_event.set()

    def recent_volatility_pct(self, exchange: str, symbol: str) -> float | None:
        """Coefficient of variation of the last ~20 mid-price samples, as a %. None if too little history."""
        history = self._mid_price_history.get((exchange, symbol))
        if history is None or len(history) < 3:
            return None
        mean = statistics.fmean(history)
        if mean <= 0:
            return None
        return statistics.pstdev(history) / mean * 100

    async def wait_for_update(self, timeout: float) -> bool:
        """Block until a quote updates or `timeout` elapses. Returns whether an update woke it."""
        try:
            await asyncio.wait_for(self._update_event.wait(), timeout=timeout)
            woke_on_update = True
        except TimeoutError:
            woke_on_update = False
        finally:
            self._update_event.clear()
        return woke_on_update

    def get_quote(self, exchange: str, market: MarketType, symbol: str) -> NormalizedQuote | None:
        return self._quotes.get((exchange, market, symbol))

    def quotes_for_symbol(self, market: MarketType, symbol: str) -> dict[str, NormalizedQuote]:
        return {ex: q for (ex, m, s), q in self._quotes.items() if m == market and s == symbol}

    def update_funding(self, snapshot: FundingSnapshot) -> None:
        self._funding[(snapshot.exchange, snapshot.symbol)] = snapshot

    def funding_for_symbol(self, symbol: str) -> dict[str, FundingSnapshot]:
        return {ex: f for (ex, s), f in self._funding.items() if s == symbol}

    def update_delivery_future(self, snapshot: DeliveryFuturesSnapshot) -> None:
        self._delivery_futures[(snapshot.exchange, snapshot.symbol)] = snapshot

    def delivery_futures_for_symbol(self, symbol: str) -> dict[str, DeliveryFuturesSnapshot]:
        return {ex: f for (ex, s), f in self._delivery_futures.items() if s == symbol}


market_data_store = MarketDataStore()
