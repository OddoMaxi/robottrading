"""DUAL-LEG REALITY VALIDATION (Phase 2F, user directive, 2026-08-23).

Phase 2D/2E only ever reality-tested the Binance side of a cross_exchange
opportunity. This module recomputes the FULL arbitrage independently from
live, real, separately-fetched data on BOTH legs — never reusing
opp.expected_profit_usd (or any other paper-engine number) as proof of
anything. No order is placed anywhere in this module; it is pure
computation over already-fetched LegSnapshot data.

Because the two legs are fetched sequentially (there is no atomic
"snapshot both exchanges at once" API), the gap between when each leg's
quote was captured is itself a real execution risk — dual_leg_latency_ms
below measures exactly that gap, and quote_age_ms measures how stale
each individual leg's quote is by the time the arbitrage is computed.
Neither figure is invented; both come from wall-clock timestamps taken
around each real HTTP call.
"""

import time
import uuid
from dataclasses import dataclass

from app.execution.binance_filters import round_down_to_step
from app.execution.reality_quote import _vwap_for_target_qty


@dataclass(slots=True)
class LegSnapshot:
    exchange: str
    side: str  # "buy" | "sell"
    best_bid: float
    best_ask: float
    depth_levels: list[tuple[float, float]]  # already filtered to the side this leg trades against (asks for a buy, bids for a sell)
    min_qty: float
    step_size: float
    tick_size: float
    min_notional: float | None
    tradable: bool
    maker_fee_rate: float | None
    taker_fee_rate: float
    fee_source: str  # "real_account_fee" | "estimated_default"
    fetch_started_at: float
    fetch_completed_at: float


@dataclass(slots=True)
class DualLegQuote:
    opportunity_id: uuid.UUID
    symbol: str
    buy_exchange: str
    sell_exchange: str
    buy_execution_price: float
    sell_execution_price: float
    executable_qty: float
    buy_valid_qty: float
    sell_valid_qty: float
    gross_spread_pct: float
    buy_fee_usd: float
    sell_fee_usd: float
    buy_slippage_pct: float
    sell_slippage_pct: float
    buy_quote_age_ms: float
    sell_quote_age_ms: float
    dual_leg_latency_ms: float
    net_profit_usd: float
    net_return_bps: float
    buy_min_notional_pass: bool
    buy_lot_size_pass: bool
    sell_min_notional_pass: bool
    sell_lot_size_pass: bool
    buy_tradable: bool
    sell_tradable: bool
    executable: bool
    reason: str | None
    buy_fee_source: str
    sell_fee_source: str
    computed_at: float


def compute_dual_leg_quote(
    opportunity_id: uuid.UUID,
    symbol: str,
    buy_leg: LegSnapshot,
    sell_leg: LegSnapshot,
    master_requested_size_usd: float,
    micro_live_cap_usdt: float,
    now: float | None = None,
) -> DualLegQuote:
    now = now if now is not None else time.time()

    desired_size_usd = max(0.0, min(master_requested_size_usd, micro_live_cap_usdt))
    buy_price = buy_leg.best_ask
    sell_price = sell_leg.best_bid

    buy_requested_qty = desired_size_usd / buy_price if buy_price > 0 else 0.0
    sell_requested_qty = desired_size_usd / sell_price if sell_price > 0 else 0.0
    # both legs must trade the SAME base-asset quantity (buy X on A, sell X on B) —
    # the smaller of the two independently-sized requests is what both sides can actually support
    requested_qty = min(buy_requested_qty, sell_requested_qty)

    buy_valid_qty = round_down_to_step(requested_qty, buy_leg.step_size)
    sell_valid_qty = round_down_to_step(requested_qty, sell_leg.step_size)
    executable_qty = min(buy_valid_qty, sell_valid_qty)

    buy_notional = executable_qty * buy_price
    sell_notional = executable_qty * sell_price

    buy_min_notional_pass = buy_leg.min_notional is None or buy_notional >= buy_leg.min_notional
    sell_min_notional_pass = sell_leg.min_notional is None or sell_notional >= sell_leg.min_notional
    buy_lot_size_pass = executable_qty > 0 and executable_qty >= buy_leg.min_qty
    sell_lot_size_pass = executable_qty > 0 and executable_qty >= sell_leg.min_qty

    gross_spread_pct = (sell_price - buy_price) / buy_price * 100 if buy_price > 0 else 0.0

    buy_vwap, buy_filled = _vwap_for_target_qty(buy_leg.depth_levels, executable_qty)
    if buy_filled <= 0 or buy_price <= 0:
        buy_slippage_pct = 0.0
    else:
        buy_slippage_pct = abs(buy_vwap - buy_price) / buy_price * 100
        if buy_filled < executable_qty:
            buy_slippage_pct = max(buy_slippage_pct, 100.0)

    sell_vwap, sell_filled = _vwap_for_target_qty(sell_leg.depth_levels, executable_qty)
    if sell_filled <= 0 or sell_price <= 0:
        sell_slippage_pct = 0.0
    else:
        sell_slippage_pct = abs(sell_vwap - sell_price) / sell_price * 100
        if sell_filled < executable_qty:
            sell_slippage_pct = max(sell_slippage_pct, 100.0)

    buy_fee_usd = buy_notional * buy_leg.taker_fee_rate
    sell_fee_usd = sell_notional * sell_leg.taker_fee_rate
    buy_slippage_cost_usd = buy_notional * (buy_slippage_pct / 100)
    sell_slippage_cost_usd = sell_notional * (sell_slippage_pct / 100)

    net_profit_usd = (sell_notional - sell_fee_usd - sell_slippage_cost_usd) - (buy_notional + buy_fee_usd + buy_slippage_cost_usd)
    net_return_bps = net_profit_usd / buy_notional * 10_000 if buy_notional > 0 else 0.0

    buy_quote_age_ms = (now - buy_leg.fetch_completed_at) * 1000
    sell_quote_age_ms = (now - sell_leg.fetch_completed_at) * 1000
    dual_leg_latency_ms = abs(sell_leg.fetch_completed_at - buy_leg.fetch_completed_at) * 1000

    checks = [
        (buy_leg.tradable, f"buy leg ({buy_leg.exchange}) not tradable"),
        (sell_leg.tradable, f"sell leg ({sell_leg.exchange}) not tradable"),
        (buy_lot_size_pass, f"buy leg quantity {executable_qty} below min_qty {buy_leg.min_qty}"),
        (sell_lot_size_pass, f"sell leg quantity {executable_qty} below min_qty {sell_leg.min_qty}"),
        (buy_min_notional_pass, f"buy notional {buy_notional:.4f} below min_notional {buy_leg.min_notional}"),
        (sell_min_notional_pass, f"sell notional {sell_notional:.4f} below min_notional {sell_leg.min_notional}"),
    ]
    reason = next((msg for passed, msg in checks if not passed), None)
    executable = reason is None
    if executable and net_profit_usd <= 0:
        executable = False
        reason = f"net_profit_usd is {net_profit_usd:.6f} (<= 0) after both legs' real fees/slippage"

    return DualLegQuote(
        opportunity_id=opportunity_id,
        symbol=symbol,
        buy_exchange=buy_leg.exchange,
        sell_exchange=sell_leg.exchange,
        buy_execution_price=buy_price,
        sell_execution_price=sell_price,
        executable_qty=executable_qty,
        buy_valid_qty=buy_valid_qty,
        sell_valid_qty=sell_valid_qty,
        gross_spread_pct=gross_spread_pct,
        buy_fee_usd=buy_fee_usd,
        sell_fee_usd=sell_fee_usd,
        buy_slippage_pct=buy_slippage_pct,
        sell_slippage_pct=sell_slippage_pct,
        buy_quote_age_ms=buy_quote_age_ms,
        sell_quote_age_ms=sell_quote_age_ms,
        dual_leg_latency_ms=dual_leg_latency_ms,
        net_profit_usd=net_profit_usd,
        net_return_bps=net_return_bps,
        buy_min_notional_pass=buy_min_notional_pass,
        buy_lot_size_pass=buy_lot_size_pass,
        sell_min_notional_pass=sell_min_notional_pass,
        sell_lot_size_pass=sell_lot_size_pass,
        buy_tradable=buy_leg.tradable,
        sell_tradable=sell_leg.tradable,
        executable=executable,
        reason=reason,
        buy_fee_source=buy_leg.fee_source,
        sell_fee_source=sell_leg.fee_source,
        computed_at=now,
    )
