"""Bybit spot WebSocket collector — v5 public "tickers" topic."""

import asyncio
import json
import logging

import websockets
from websockets.asyncio.client import ClientConnection

from app.collectors.base import MarketDataCollector
from app.market_data.normalizer import normalize_bybit_ticker
from app.market_data.store import MarketDataStore
from app.market_data.symbols import to_native_symbol

logger = logging.getLogger(__name__)

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/spot"


class BybitCollector(MarketDataCollector):
    exchange = "bybit"

    async def _run_once(self, store: MarketDataStore) -> None:
        async with websockets.connect(BYBIT_WS_URL) as ws:
            topics = [f"tickers.{to_native_symbol('bybit', s)}" for s in self.symbols]
            await ws.send(json.dumps({"op": "subscribe", "args": topics}))
            logger.info("bybit collector connected (%d symbols)", len(self.symbols))

            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    topic = message.get("topic", "")
                    if topic.startswith("tickers."):
                        quote = normalize_bybit_ticker(message.get("data", {}), message.get("ts"))
                        if quote:
                            store.update_quote(quote)
            finally:
                keepalive.cancel()

    async def _keepalive(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))
