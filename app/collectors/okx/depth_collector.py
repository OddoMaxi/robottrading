"""OKX spot WebSocket collector — public "books5" (top-5-levels) channel.

Opportunity Expansion spec, Step 2 (user directive, 2026-08-21) — real
multi-level depth for OKX, replacing the single-level VWAP top-of-book
fallback everywhere app.engines._shared reads app.market_data.store's order
books. Separate from OkxCollector (which still owns the "tickers" stream
every engine already depends on) — purely additive, same pattern as
BinanceDepthCollector.

"books5" pushes a full top-5 snapshot on every update (no incremental
reconciliation/sequence tracking needed), the same deliberate tradeoff
already made for Binance's depth20@100ms — a correct 5-level book already
replaces the single-level approximation, for much less risk than a fully
sequence-tracked local book.
"""

import asyncio
import json
import logging

import websockets
from websockets.asyncio.client import ClientConnection

from app.collectors.base import MarketDataCollector
from app.market_data.normalizer import normalize_okx_books5
from app.market_data.store import MarketDataStore
from app.market_data.symbols import to_common_symbol, to_native_symbol

logger = logging.getLogger(__name__)

OKX_WS_URL = "wss://ws.okx.com:8443/ws/v5/public"


class OkxDepthCollector(MarketDataCollector):
    exchange = "okx"

    async def _run_once(self, store: MarketDataStore) -> None:
        async with websockets.connect(OKX_WS_URL) as ws:
            args = [{"channel": "books5", "instId": to_native_symbol("okx", s)} for s in self.symbols]
            await ws.send(json.dumps({"op": "subscribe", "args": args}))
            logger.info("okx depth collector connected (%d symbols)", len(self.symbols))

            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    if raw == "pong":
                        continue
                    message = json.loads(raw)
                    if message.get("event") == "error":
                        logger.warning("okx depth subscribe error: %s", message.get("msg"))
                        continue
                    arg = message.get("arg", {})
                    if arg.get("channel") != "books5":
                        continue
                    inst_id = arg.get("instId")
                    if not inst_id:
                        continue
                    symbol_common = to_common_symbol("okx", inst_id)
                    for item in message.get("data", []):
                        book = normalize_okx_books5(symbol_common, item)
                        if book:
                            store.update_order_book(book)
            finally:
                keepalive.cancel()

    async def _keepalive(self, ws: ClientConnection) -> None:
        # OKX drops idle connections after ~30s of silence; a text "ping" resets that.
        while True:
            await asyncio.sleep(20)
            await ws.send("ping")
