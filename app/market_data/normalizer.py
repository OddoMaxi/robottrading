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


def normalize_okx_books5(symbol_common: str, data: dict) -> OrderBook | None:
    """OKX's "books5" channel — a full top-5-levels snapshot pushed on every
    update (no incremental reconciliation needed), same semantics as
    Binance's depth20@100ms. Opportunity Expansion spec, Step 2 (user
    directive, 2026-08-21): real depth for OKX, not just top-of-book. Each
    level is [price, size, deprecated_liquidated_orders, order_count] — the
    trailing two fields are ignored."""
    bids, asks = data.get("bids"), data.get("asks")
    if not bids or not asks:
        return None
    return OrderBook(
        exchange="okx",
        symbol=symbol_common,
        bids=[OrderBookLevel(price=float(p), quantity=float(q)) for p, q, *_ in bids],
        asks=[OrderBookLevel(price=float(p), quantity=float(q)) for p, q, *_ in asks],
        timestamp=time.time(),
    )


def apply_bybit_depth_delta(levels: dict[float, float], updates: list[list[str]]) -> None:
    """Mutates `levels` (price -> quantity) in place per Bybit v5's
    orderbook.50 delta semantics: a size of "0" removes that price level
    entirely, any other size upserts it. `updates` is empty for a side that
    didn't change in this delta message, so this is a safe no-op then."""
    for price_str, qty_str in updates:
        price = float(price_str)
        qty = float(qty_str)
        if qty == 0.0:
            levels.pop(price, None)
        else:
            levels[price] = qty


def build_order_book_from_levels(
    exchange: str, symbol_common: str, bids: dict[float, float], asks: dict[float, float], max_levels: int
) -> OrderBook | None:
    """Sorts and truncates a maintained {price: quantity} book (Bybit's
    snapshot+delta local reconstruction) into the same OrderBook shape the
    single-snapshot exchanges (Binance, OKX) produce directly — VWAP
    simulation and every engine that reads app.market_data.store's order
    books never needs to know which exchange used which wire protocol."""
    if not bids or not asks:
        return None
    sorted_bids = sorted(bids.items(), key=lambda kv: kv[0], reverse=True)[:max_levels]
    sorted_asks = sorted(asks.items(), key=lambda kv: kv[0])[:max_levels]
    return OrderBook(
        exchange=exchange,
        symbol=symbol_common,
        bids=[OrderBookLevel(price=p, quantity=q) for p, q in sorted_bids],
        asks=[OrderBookLevel(price=p, quantity=q) for p, q in sorted_asks],
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
