"""Shared in-memory market data store (V1: single process).

Collectors write into it, engines read from it. A plain dict is safe here
because asyncio is single-threaded and cooperative — each update is one
atomic dict assignment, never interleaved with a read.
"""

from dataclasses import dataclass

from app.config.constants import MarketType
from app.market_data.normalizer import NormalizedQuote


@dataclass(slots=True)
class FundingSnapshot:
    exchange: str
    symbol: str  # common form, e.g. "BTC/USDT" — the perpetual on that asset/quote
    funding_rate: float
    next_funding_time: float  # epoch seconds
    mark_price: float
    index_price: float
    received_at: float


class MarketDataStore:
    def __init__(self) -> None:
        self._quotes: dict[tuple[str, MarketType, str], NormalizedQuote] = {}
        self._funding: dict[tuple[str, str], FundingSnapshot] = {}

    def update_quote(self, quote: NormalizedQuote) -> None:
        self._quotes[(quote.exchange, quote.market, quote.symbol)] = quote

    def get_quote(self, exchange: str, market: MarketType, symbol: str) -> NormalizedQuote | None:
        return self._quotes.get((exchange, market, symbol))

    def quotes_for_symbol(self, market: MarketType, symbol: str) -> dict[str, NormalizedQuote]:
        return {ex: q for (ex, m, s), q in self._quotes.items() if m == market and s == symbol}

    def update_funding(self, snapshot: FundingSnapshot) -> None:
        self._funding[(snapshot.exchange, snapshot.symbol)] = snapshot

    def funding_for_symbol(self, symbol: str) -> dict[str, FundingSnapshot]:
        return {ex: f for (ex, s), f in self._funding.items() if s == symbol}


market_data_store = MarketDataStore()
