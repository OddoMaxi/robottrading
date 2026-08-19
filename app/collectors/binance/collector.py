"""Binance spot WebSocket collector — combined bookTicker stream."""

import json
import logging

import websockets

from app.collectors.base import MarketDataCollector
from app.market_data.normalizer import normalize_binance_book_ticker
from app.market_data.store import MarketDataStore
from app.market_data.symbols import to_native_symbol

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"


class BinanceCollector(MarketDataCollector):
    exchange = "binance"

    async def _run_once(self, store: MarketDataStore) -> None:
        streams = "/".join(f"{to_native_symbol('binance', s).lower()}@bookTicker" for s in self.symbols)
        url = f"{BINANCE_WS_URL}?streams={streams}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            logger.info("binance collector connected (%d symbols)", len(self.symbols))
            async for raw in ws:
                message = json.loads(raw)
                data = message.get("data")
                if not data:
                    continue
                store.update_quote(normalize_binance_book_ticker(data))
