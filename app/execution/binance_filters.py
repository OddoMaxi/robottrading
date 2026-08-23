"""Binance exchange filters — real per-symbol trading constraints and an
EXECUTABLE / NOT_EXECUTABLE validator (Phase 2D, item 4, user directive,
2026-08-23).

Pure parsing/validation module: every function here takes already-fetched
`exchangeInfo` JSON (from BinanceAccountClient.get_exchange_info) and
returns structured, testable results. No network I/O in this file.
"""

import math
from dataclasses import dataclass


class SymbolNotFound(Exception):
    pass


@dataclass(slots=True)
class SymbolRules:
    symbol: str
    status: str  # "TRADING" means live-tradable right now
    base_asset: str
    quote_asset: str
    base_precision: int
    quote_precision: int
    min_qty: float
    max_qty: float
    step_size: float
    tick_size: float
    min_notional: float | None  # None means Binance didn't publish one for this symbol
    order_types: list[str]
    is_spot_trading_allowed: bool


def parse_symbol_rules(exchange_info: dict, symbol: str) -> SymbolRules:
    for entry in exchange_info.get("symbols", []):
        if entry.get("symbol") == symbol:
            filters = {f["filterType"]: f for f in entry.get("filters", [])}
            lot = filters.get("LOT_SIZE", {})
            price = filters.get("PRICE_FILTER", {})
            notional_filter = filters.get("NOTIONAL") or filters.get("MIN_NOTIONAL")
            min_notional = None
            if notional_filter is not None:
                raw = notional_filter.get("minNotional")
                min_notional = float(raw) if raw is not None else None
            return SymbolRules(
                symbol=symbol,
                status=entry.get("status", "UNKNOWN"),
                base_asset=entry.get("baseAsset", ""),
                quote_asset=entry.get("quoteAsset", ""),
                base_precision=int(entry.get("baseAssetPrecision", 8)),
                quote_precision=int(entry.get("quoteAssetPrecision", 8)),
                min_qty=float(lot.get("minQty", 0.0)),
                max_qty=float(lot.get("maxQty", float("inf"))),
                step_size=float(lot.get("stepSize", 0.0)),
                tick_size=float(price.get("tickSize", 0.0)),
                min_notional=min_notional,
                order_types=list(entry.get("orderTypes", [])),
                is_spot_trading_allowed=bool(entry.get("isSpotTradingAllowed", False)),
            )
    raise SymbolNotFound(symbol)


def round_down_to_step(value: float, step: float) -> float:
    """Binance rejects a quantity that isn't an exact multiple of
    stepSize — round DOWN (never up, that could exceed available
    balance) to the nearest valid step."""
    if step <= 0:
        return value
    steps = math.floor(value / step + 1e-9)
    return round(steps * step, 12)


@dataclass(slots=True)
class FilterValidation:
    executable: bool
    reason: str | None  # exact reason when not executable, per item 4's "EXECUTABLE or NOT_EXECUTABLE + exact_reason"
    exchange_valid_qty: float
    min_notional_pass: bool
    lot_size_pass: bool
    balance_pass: bool
    status_pass: bool
    order_type_pass: bool


def validate_order(
    rules: SymbolRules,
    side: str,
    price: float,
    requested_qty: float,
    available_quote_balance_usdt: float,
    order_type: str = "MARKET",
) -> FilterValidation:
    """Answers EXECUTABLE / NOT_EXECUTABLE + exact_reason for a would-be
    order against REAL Binance constraints — no order is placed here."""
    status_pass = rules.status == "TRADING" and rules.is_spot_trading_allowed
    order_type_pass = order_type in rules.order_types

    exchange_valid_qty = round_down_to_step(requested_qty, rules.step_size)
    lot_size_pass = rules.min_qty <= exchange_valid_qty <= rules.max_qty and exchange_valid_qty > 0

    notional = exchange_valid_qty * price
    min_notional_pass = rules.min_notional is None or notional >= rules.min_notional

    cost_usdt = notional if side.upper() == "BUY" else 0.0
    balance_pass = cost_usdt <= available_quote_balance_usdt

    if not status_pass:
        return FilterValidation(False, f"symbol status is {rules.status}, not TRADING", exchange_valid_qty, min_notional_pass, lot_size_pass, balance_pass, status_pass, order_type_pass)
    if not order_type_pass:
        return FilterValidation(False, f"order type {order_type} not in allowed types {rules.order_types}", exchange_valid_qty, min_notional_pass, lot_size_pass, balance_pass, status_pass, order_type_pass)
    if not lot_size_pass:
        return FilterValidation(
            False,
            f"quantity {exchange_valid_qty} outside LOT_SIZE bounds [{rules.min_qty}, {rules.max_qty}] (step {rules.step_size})",
            exchange_valid_qty, min_notional_pass, lot_size_pass, balance_pass, status_pass, order_type_pass,
        )
    if not min_notional_pass:
        return FilterValidation(
            False,
            f"notional {notional:.4f} USDT below MIN_NOTIONAL {rules.min_notional} USDT",
            exchange_valid_qty, min_notional_pass, lot_size_pass, balance_pass, status_pass, order_type_pass,
        )
    if not balance_pass:
        return FilterValidation(
            False,
            f"cost {cost_usdt:.4f} USDT exceeds available balance {available_quote_balance_usdt:.4f} USDT",
            exchange_valid_qty, min_notional_pass, lot_size_pass, balance_pass, status_pass, order_type_pass,
        )
    return FilterValidation(True, None, exchange_valid_qty, min_notional_pass, lot_size_pass, balance_pass, status_pass, order_type_pass)
