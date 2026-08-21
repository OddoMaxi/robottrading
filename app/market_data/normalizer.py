"""Common data model that every exchange payload gets converted into (section 9)."""

import time
from dataclasses import dataclass

from app.config.constants import MarketType
from app.market_data.orderbook import OrderBook, OrderBookLevel
from app.market_data.symbols import to_common_symbol


@dataclass(slots=True)
class NormalizedQuote:
    exchange: str
    market: MarketType
    symbol: str  # e.g. "BTC/USDT"
    bid: float
    ask: float
    bid_quantity: float
    ask_quantity: float
    exchange_timestamp: float  # epoch seconds, from the exchange payload
    received_at: float  # epoch seconds, local reception time


def normalize_binance_book_ticker(data: dict) -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange="binance",
        market=MarketType.SPOT,
        symbol=to_common_symbol("binance", data["s"]),
        bid=float(data["b"]),
        ask=float(data["a"]),
        bid_quantity=float(data["B"]),
        ask_quantity=float(data["A"]),
        # Binance's spot bookTicker stream carries no per-event exchange timestamp.
        exchange_timestamp=now,
        received_at=now,
    )


def normalize_okx_ticker(item: dict) -> NormalizedQuote | None:
    bid_px, ask_px = item.get("bidPx"), item.get("askPx")
    if not bid_px or not ask_px:
        return None
    return NormalizedQuote(
        exchange="okx",
        market=MarketType.SPOT,
        symbol=to_common_symbol("okx", item["instId"]),
        bid=float(bid_px),
        ask=float(ask_px),
        bid_quantity=float(item.get("bidSz") or 0),
        ask_quantity=float(item.get("askSz") or 0),
        exchange_timestamp=float(item["ts"]) / 1000,
        received_at=time.time(),
    )


def normalize_binance_partial_depth(symbol_common: str, data: dict) -> OrderBook | None:
    """Binance's <symbol>@depth20@100ms stream — a full top-20-levels
    snapshot on every update (no incremental reconciliation needed, unlike
    the diff-depth stream). Reality Engine spec, sections 7-8: real
    multi-level depth for VWAP instead of a single top-of-book level.
    Levels arrive already sorted (bids highest-first, asks lowest-first)."""
    bids, asks = data.get("bids"), data.get("asks")
    if not bids or not asks:
        return None
    return OrderBook(
        exchange="binance",
        symbol=symbol_common,
        bids=[OrderBookLevel(price=float(p), quantity=float(q)) for p, q in bids],
        asks=[OrderBookLevel(price=float(p), quantity=float(q)) for p, q in asks],
        timestamp=time.time(),
    )


def normalize_bybit_orderbook(data: dict, ts_ms: float | None) -> NormalizedQuote | None:
    # Bybit's v5 "orderbook.1" (L1) topic sends a full snapshot — both sides
    # — on every update, unlike "tickers" which stopped carrying
    # bid1Price/ask1Price at all (confirmed live, 2026-08-20).
    bids, asks = data.get("b"), data.get("a")
    if not bids or not asks:
        return None
    bid_px, bid_qty = bids[0]
    ask_px, ask_qty = asks[0]
    return NormalizedQuote(
        exchange="bybit",
        market=MarketType.SPOT,
        symbol=to_common_symbol("bybit", data["s"]),
        bid=float(bid_px),
        ask=float(ask_px),
        bid_quantity=float(bid_qty),
        ask_quantity=float(ask_qty),
        exchange_timestamp=(float(ts_ms) / 1000) if ts_ms else time.time(),
        received_at=time.time(),
    )
