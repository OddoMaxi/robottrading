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
# Bybit v5 public WS rejects a subscribe request outright — for *every*
# symbol in that request, not just the bad one — if it contains more than
# 10 args, or if even one symbol in the batch isn't listed on Bybit (our
# symbol list isn't Bybit-specific: e.g. BNB/BTC or any FDUSD pair). Found
# in production: batching 10-at-a-time still dropped valid symbols like
# BTC/USDT whenever they landed in the same alphabetical batch as an
# unlisted one. One symbol per subscribe message fully isolates failures.
MAX_ARGS_PER_SUBSCRIBE = 1


class BybitCollector(MarketDataCollector):
    exchange = "bybit"

    async def _run_once(self, store: MarketDataStore) -> None:
        async with websockets.connect(BYBIT_WS_URL) as ws:
            topics = [f"tickers.{to_native_symbol('bybit', s)}" for s in self.symbols]
            # Sent as separate messages (not awaiting each ack before the
            # next) — acks and ticker data can interleave once earlier
            # batches are live, so acks are handled in the main loop below
            # instead of assuming a strict one-recv-per-subscribe order.
            for i in range(0, len(topics), MAX_ARGS_PER_SUBSCRIBE):
                batch = topics[i : i + MAX_ARGS_PER_SUBSCRIBE]
                await ws.send(json.dumps({"op": "subscribe", "args": batch}))
                await asyncio.sleep(0.1)  # avoid tripping Bybit's WS op rate limit
            logger.info("bybit collector connected (%d symbols)", len(self.symbols))

            keepalive = asyncio.create_task(self._keepalive(ws))
            debug_left = 5
            try:
                async for raw in ws:
                    if debug_left > 0:
                        logger.warning("bybit raw: %s", raw[:300])
                        debug_left -= 1
                    message = json.loads(raw)
                    if message.get("op") == "subscribe" and not message.get("success", True):
                        logger.warning("bybit subscribe batch rejected: %s", message.get("ret_msg"))
                        continue
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
