"""Bybit spot WebSocket collector — v5 public "orderbook.50" (50-level) topic.

Opportunity Expansion spec, Step 2 (user directive, 2026-08-21) — real
multi-level depth for Bybit, replacing the single-level VWAP top-of-book
fallback everywhere app.engines._shared reads app.market_data.store's order
books. Separate from BybitCollector (which still owns "orderbook.1" for
the top-of-book quote every engine already depends on).

Unlike Binance's depth20@100ms and OKX's books5 (both full snapshots on
every push), Bybit's deeper orderbook topics use snapshot+delta: the first
message per symbol is a full "snapshot", every message after that is a
"delta" containing only the price levels that changed since (a size of "0"
means that level was removed). This collector maintains that reconstructed
book locally per symbol (via the pure, unit-tested
apply_bybit_depth_delta/build_order_book_from_levels helpers in
app.market_data.normalizer) and republishes it to the shared store on every
update — the shared store itself only ever holds the final, ready-to-read
OrderBook shape, same as the other two collectors.
"""

import asyncio
import json
import logging

import websockets
from websockets.asyncio.client import ClientConnection

from app.collectors.base import MarketDataCollector
from app.market_data.normalizer import apply_bybit_depth_delta, build_order_book_from_levels
from app.market_data.store import MarketDataStore
from app.market_data.symbols import to_common_symbol, to_native_symbol

logger = logging.getLogger(__name__)

BYBIT_WS_URL = "wss://stream.bybit.com/v5/public/spot"
DEPTH_TOPIC_PREFIX = "orderbook.50."
PUBLISHED_DEPTH_LEVELS = 20
# Same constraint BybitCollector already found in production: a subscribe
# batch is rejected in full if it contains more than 10 args, or if even
# one symbol in it isn't listed on Bybit. One symbol per message isolates
# failures to that symbol alone.
MAX_ARGS_PER_SUBSCRIBE = 1


class BybitDepthCollector(MarketDataCollector):
    exchange = "bybit"

    async def _run_once(self, store: MarketDataStore) -> None:
        # native_symbol -> {"bids": {price: qty}, "asks": {price: qty}} —
        # local reconstruction of each symbol's book, rebuilt fresh on every
        # "snapshot" message and merged in place on every "delta".
        books: dict[str, dict[str, dict[float, float]]] = {}

        async with websockets.connect(BYBIT_WS_URL) as ws:
            topics = [f"{DEPTH_TOPIC_PREFIX}{to_native_symbol('bybit', s)}" for s in self.symbols]
            for i in range(0, len(topics), MAX_ARGS_PER_SUBSCRIBE):
                batch = topics[i : i + MAX_ARGS_PER_SUBSCRIBE]
                await ws.send(json.dumps({"op": "subscribe", "args": batch}))
                await asyncio.sleep(0.1)  # avoid tripping Bybit's WS op rate limit
            logger.info("bybit depth collector connected (%d symbols)", len(self.symbols))

            keepalive = asyncio.create_task(self._keepalive(ws))
            try:
                async for raw in ws:
                    message = json.loads(raw)
                    if message.get("op") == "subscribe" and not message.get("success", True):
                        logger.warning("bybit depth subscribe batch rejected: %s", message.get("ret_msg"))
                        continue
                    topic = message.get("topic", "")
                    if not topic.startswith(DEPTH_TOPIC_PREFIX):
                        continue
                    data = message.get("data", {})
                    native_symbol = data.get("s")
                    if not native_symbol:
                        continue

                    book_state = books.setdefault(native_symbol, {"bids": {}, "asks": {}})
                    if message.get("type") == "snapshot":
                        book_state["bids"] = {float(p): float(q) for p, q in data.get("b", [])}
                        book_state["asks"] = {float(p): float(q) for p, q in data.get("a", [])}
                    else:
                        apply_bybit_depth_delta(book_state["bids"], data.get("b", []))
                        apply_bybit_depth_delta(book_state["asks"], data.get("a", []))

                    order_book = build_order_book_from_levels(
                        "bybit",
                        to_common_symbol("bybit", native_symbol),
                        book_state["bids"],
                        book_state["asks"],
                        PUBLISHED_DEPTH_LEVELS,
                    )
                    if order_book:
                        store.update_order_book(order_book)
            finally:
                keepalive.cancel()

    async def _keepalive(self, ws: ClientConnection) -> None:
        while True:
            await asyncio.sleep(20)
            await ws.send(json.dumps({"op": "ping"}))
