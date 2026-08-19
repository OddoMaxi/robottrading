"""Common interface every exchange collector implements (section 8 — Market Data Engine)."""

import asyncio
import logging

from app.market_data.store import MarketDataStore

logger = logging.getLogger(__name__)


class MarketDataCollector:
    """WebSocket connector for one exchange.

    Subclasses implement `_run_once`, which connects, subscribes to
    `self.symbols`, and pushes NormalizedQuote updates into `store` until the
    connection drops or raises. `run` wraps that with reconnect/backoff.
    """

    exchange: str

    def __init__(self, symbols: list[str]) -> None:
        self.symbols = symbols

    async def run(self, store: MarketDataStore) -> None:
        backoff = 1.0
        while True:
            try:
                await self._run_once(store)
                backoff = 1.0  # clean disconnect — reset backoff before reconnecting
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("%s collector crashed, reconnecting in %.0fs", self.exchange, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30.0)

    async def _run_once(self, store: MarketDataStore) -> None:
        raise NotImplementedError
