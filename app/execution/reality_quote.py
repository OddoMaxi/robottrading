"""REALITY QUOTE / DRY-RUN evaluator (Phase 2D, item 5, user directive,
2026-08-23).

For every opportunity MASTER would have wanted to execute, computes what
would ACTUALLY happen against live Binance data and real exchange
constraints — master_requested_size, exchange_valid_size, best bid/ask,
available depth, estimated fees/slippage, estimated net profit after real
constraints, and the pass/fail breakdown, ending in EXECUTABLE /
NOT_EXECUTABLE. No order is placed anywhere in this module — it is pure
computation over already-fetched market data (book ticker + depth) and
already-parsed SymbolRules (app.execution.binance_filters).

Never manufactures a positive result: PAPER capital is never consulted
here (only micro_live_cap_usdt and the real account balance passed in by
the caller), and thresholds are not adjustable from this module.
"""

import time
import uuid
from dataclasses import dataclass

from app.execution.binance_filters import SymbolRules, validate_order

DEFAULT_TAKER_FEE_RATE = 0.001  # 0.1% — Binance's standard spot taker fee; used only when a real fee could not be fetched (item 4: "fees where determinable")


@dataclass(slots=True)
class RealityQuote:
    opportunity_id: uuid.UUID
    symbol: str
    side: str
    master_requested_size_usd: float
    exchange_valid_size_usd: float
    best_bid: float
    best_ask: float
    available_depth_usd: float
    estimated_fees_usd: float
    estimated_slippage_pct: float
    estimated_net_profit_after_real_constraints_usd: float
    min_notional_pass: bool
    lot_size_pass: bool
    balance_pass: bool
    executable: bool
    reason: str | None
    fee_source: str  # "real" | "estimated_default"
    computed_at: float


def _vwap_for_target_qty(levels: list[tuple[float, float]], target_qty: float) -> tuple[float, float]:
    """levels: [(price, qty), ...] ordered from best price outward.
    Returns (vwap_price, filled_qty) — filled_qty < target_qty means the
    visible book depth couldn't fully absorb the size."""
    remaining = target_qty
    cost = 0.0
    filled = 0.0
    for price, qty in levels:
        if remaining <= 0:
            break
        take = min(remaining, qty)
        cost += take * price
        filled += take
        remaining -= take
    if filled == 0:
        return 0.0, 0.0
    return cost / filled, filled


def compute_reality_quote(
    opportunity_id: uuid.UUID,
    symbol: str,
    side: str,
    master_requested_size_usd: float,
    gross_spread_pct: float,
    rules: SymbolRules,
    best_bid: float,
    best_ask: float,
    depth_levels: list[tuple[float, float]],
    account_balance_usdt: float,
    micro_live_cap_usdt: float,
    taker_fee_rate: float = DEFAULT_TAKER_FEE_RATE,
    fee_source: str = "estimated_default",
    now: float | None = None,
) -> RealityQuote:
    now = now if now is not None else time.time()
    reference_price = best_ask if side.upper() == "BUY" else best_bid

    desired_size_usd = max(0.0, min(master_requested_size_usd, micro_live_cap_usdt))
    requested_qty = desired_size_usd / reference_price if reference_price > 0 else 0.0

    validation = validate_order(rules, side, reference_price, requested_qty, account_balance_usdt)
    exchange_valid_size_usd = validation.exchange_valid_qty * reference_price

    available_depth_usd = sum(price * qty for price, qty in depth_levels)
    vwap, filled_qty = _vwap_for_target_qty(depth_levels, validation.exchange_valid_qty)
    if filled_qty <= 0 or reference_price <= 0:
        estimated_slippage_pct = 0.0
    else:
        estimated_slippage_pct = abs(vwap - reference_price) / reference_price * 100
        if filled_qty < validation.exchange_valid_qty:
            estimated_slippage_pct = max(estimated_slippage_pct, 100.0)  # visible depth insufficient — flag as fully unabsorbable

    estimated_fees_usd = exchange_valid_size_usd * taker_fee_rate
    slippage_cost_usd = exchange_valid_size_usd * (estimated_slippage_pct / 100)
    gross_profit_usd = exchange_valid_size_usd * (gross_spread_pct / 100)
    estimated_net_profit_after_real_constraints_usd = gross_profit_usd - estimated_fees_usd - slippage_cost_usd

    executable = validation.executable
    reason = validation.reason
    if executable and estimated_net_profit_after_real_constraints_usd <= 0:
        executable = False
        reason = f"estimated net profit after real fees/slippage is {estimated_net_profit_after_real_constraints_usd:.4f} USD (<= 0)"

    return RealityQuote(
        opportunity_id=opportunity_id,
        symbol=symbol,
        side=side,
        master_requested_size_usd=master_requested_size_usd,
        exchange_valid_size_usd=exchange_valid_size_usd,
        best_bid=best_bid,
        best_ask=best_ask,
        available_depth_usd=available_depth_usd,
        estimated_fees_usd=estimated_fees_usd,
        estimated_slippage_pct=estimated_slippage_pct,
        estimated_net_profit_after_real_constraints_usd=estimated_net_profit_after_real_constraints_usd,
        min_notional_pass=validation.min_notional_pass,
        lot_size_pass=validation.lot_size_pass,
        balance_pass=validation.balance_pass,
        executable=executable,
        reason=reason,
        fee_source=fee_source,
        computed_at=now,
    )
