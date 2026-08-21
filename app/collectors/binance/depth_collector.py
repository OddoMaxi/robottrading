"""Binance spot WebSocket collector — partial book depth (top 20 levels).

Reality Engine spec, sections 7-8. Separate from BinanceCollector (which
still owns the top-of-book bookTicker stream every engine already
depends on) — this is purely additive, so a bug here can't take down
detection the way a mistake in the working collector could.

Uses <symbol>@depth20@100ms, which sends a complete top-20 snapshot on
every update rather than the incremental diff-depth stream — no
sequence-number reconciliation needed, at the cost of not being a fully
gapless order book. That tradeoff is deliberate for this first pass:
section 7's full sequence-tracked local book is real future work, but a
correct 20-level snapshot already replaces the single-level VWAP
approximation everywhere it's wired in, for much less risk.
"""

import json
import logging

import websockets

from app.collectors.base import MarketDataCollector
from app.market_data.normalizer import normalize_binance_partial_depth
from app.market_data.store import MarketDataStore
from app.market_data.symbols import to_common_symbol, to_native_symbol

logger = logging.getLogger(__name__)

BINANCE_WS_URL = "wss://stream.binance.com:9443/stream"


class BinanceDepthCollector(MarketDataCollector):
    exchange = "binance"

    async def _run_once(self, store: MarketDataStore) -> None:
        streams = "/".join(f"{to_native_symbol('binance', s).lower()}@depth20@100ms" for s in self.symbols)
        url = f"{BINANCE_WS_URL}?streams={streams}"
        async with websockets.connect(url, ping_interval=20, ping_timeout=20) as ws:
            logger.info("binance depth collector connected (%d symbols)", len(self.symbols))
            async for raw in ws:
                message = json.loads(raw)
                stream = message.get("stream", "")
                data = message.get("data")
                if not data or "@" not in stream:
                    continue
                native_symbol = stream.split("@")[0].upper()
                symbol_common = to_common_symbol("binance", native_symbol)
                book = normalize_binance_partial_depth(symbol_common, data)
                if book:
                    store.update_order_book(book)
