"""OKX spot WebSocket collector — public "tickers" channel."""

import asyncio
import json
import logging

import websockets
from websockets.asyncio.client import ClientConnection

from app.collectors.base import MarketDataCollector
from app.market_data.normalizer import normalize_okx_ticker
from app.market_data.store import MarketDataStore
from app.market_data.symbols import to_native_symbol

logger = logging.getLogger(__name__)

OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


class OkxCollector(MarketDataCollector):
    exchange = "okx"

    async def _run_once(self, store: MarketDataStore) -> None:
        async with websockets.connect(OKX_WS_URL) as ws:
            args = [{"channel": "tickers", "instId": to_native_symbol("okx", s)} for s in self.symbols]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            logger.info("okx collector connected (%d symbols)", len(self.symbols))

            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    message = json.loads(raw)
                    for item in message.get("data", []):
                        quote = normalize_okx_ticker(item)
                        if quote:
                            store.update_quote(quote)
            finally:
                keepalive.cancel()

    async def _keepalive(self, ws: ClientConnection) -> None:
        # OKX drops idle connections after ~30s of silence; a text "ping" resets that.
        while True:
            await asyncio.sleep(20)
            await ws.send("ping")
