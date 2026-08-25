"""LIVE TRADING DASHBOARD — real-money observability (user directive,
2026-08-25) — READ-ONLY. Aggregates the two real-money ledgers
(live_arbitrage_executions, inventory_constitution_executions) plus
fresh real exchange balances into exactly what the new "LIVE TRADING"
dashboard page needs. Never touches paper/simulation tables — this
module's numbers come exclusively from real fills and real balances.

Two known, disclosed simplifications (both real limitations of what is
currently persisted, not modeling choices made for convenience):

1. INVENTORY_MISSING/BELOW_MIN_NOTIONAL/EDGE_TOO_LOW-style pre-execution
   classification counts (the "opportunity funnel") were only ever kept
   in-memory by the one-off orchestration scripts that ran live sessions
   -- never persisted anywhere. The funnel here is built from what IS
   durable: the two ledgers' own outcome/status columns. A true
   "SCANS -> NET POSITIVE -> CONFIRMED_SHORT_TERM" funnel is not
   reconstructable after the fact.
2. Inventory cost basis is a simple weighted average across every
   recorded BUY fill for a given (asset, exchange) pair -- not a
   FIFO/LIFO ledger tracking which specific units are still held after
   many cycles of partial selling. It answers "what has this asset
   typically cost to acquire on this exchange", not an exact accounting
   cost basis for the exact units currently in the wallet.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import InventoryConstitutionRecord, LiveArbitrageExecutionRecord

ARBITRAGE_TERMINAL_OUTCOMES = {
    "both_filled",
    "buy_only_neutralized",
    "neutralization_failed",
    "unknown_buy_leg",
    "unknown_sell_leg",
    "no_fill",
    "no_trade_unprofitable",
    "no_trade_refused",
}


@dataclass(slots=True)
class LiveTradeRow:
    at: datetime
    symbol: str
    buy_exchange: str
    sell_exchange: str
    outcome: str
    notional_usdt: float | None
    predicted_net_usd: float | None
    actual_net_usd: float | None
    buy_filled_qty: float
    buy_avg_price: float | None
    sell_filled_qty: float
    sell_avg_price: float | None
    total_fees_usd: float | None
    latency_ms: float | None


@dataclass(slots=True)
class RealPnlBreakdown:
    session_pnl_usd: float | None  # None when no active/recent session is known (no status-file session_start)
    today_pnl_usd: float
    total_pnl_usd: float
    pnl_per_hour_usd: float | None  # session-relative; None when session_pnl_usd is None
    average_pnl_per_trade_usd: float | None
    best_trade: LiveTradeRow | None
    worst_trade: LiveTradeRow | None
    win_rate_pct: float | None
    predicted_total_pnl_usd: float
    actual_total_pnl_usd: float
    prediction_error_usd: float
    average_prediction_error_usd: float | None
    max_prediction_error_usd: float | None


@dataclass(slots=True)
class TradeCounts:
    complete_arbitrages: int  # both_filled
    successful: int  # both_filled AND actual_net_pnl_usd > 0
    failed: int  # buy_only_neutralized / neutralization_failed / unknown_*
    aborted: int  # no_fill / no_trade_unprofitable / no_trade_refused
    neutralizations: int  # buy_only_neutralized + neutralization_failed
    unhedged_incidents: int  # neutralization_failed only


@dataclass(slots=True)
class InventoryConstitutionSummary:
    total_constitutions: int
    new_constitutions: int  # first constitution ever recorded for that (symbol, sell_exchange)
    recycling_constitutions: int  # any subsequent one -- see module docstring, heuristic not a persisted flag
    total_inventory_cost_usd: float  # sum of USDT-denominated fees only (a base-asset fee never touched USDT)


@dataclass(slots=True)
class InventoryPosition:
    symbol: str
    exchange: str
    quantity: float
    current_price_usdt: float | None
    value_usdt: float | None
    cost_basis_usdt_per_unit: float | None
    unrealized_pnl_usd: float | None
    status: str  # READY | LOW | DUST | ACTIVE


def compute_trade_counts(arb_rows: list[LiveArbitrageExecutionRecord]) -> TradeCounts:
    complete = [r for r in arb_rows if r.outcome == "both_filled"]
    successful = sum(1 for r in complete if r.actual_net_pnl_usd is not None and float(r.actual_net_pnl_usd) > 0)
    failed = sum(1 for r in arb_rows if r.outcome in ("buy_only_neutralized", "neutralization_failed", "unknown_buy_leg", "unknown_sell_leg"))
    aborted = sum(1 for r in arb_rows if r.outcome in ("no_fill", "no_trade_unprofitable", "no_trade_refused"))
    neutralizations = sum(1 for r in arb_rows if r.outcome in ("buy_only_neutralized", "neutralization_failed"))
    unhedged = sum(1 for r in arb_rows if r.outcome == "neutralization_failed")
    return TradeCounts(
        complete_arbitrages=len(complete), successful=successful, failed=failed, aborted=aborted,
        neutralizations=neutralizations, unhedged_incidents=unhedged,
    )


def _to_trade_row(r: LiveArbitrageExecutionRecord) -> LiveTradeRow:
    total_fees = (float(r.buy_fees_usd) if r.buy_fees_usd is not None else 0.0) + (float(r.sell_fees_usd) if r.sell_fees_usd is not None else 0.0)
    latency = None
    if r.buy_latency_ms is not None and r.sell_latency_ms is not None:
        latency = float(r.buy_latency_ms) + float(r.sell_latency_ms)
    notional = float(r.buy_avg_fill_price) * float(r.buy_filled_qty) if r.buy_avg_fill_price is not None else None
    return LiveTradeRow(
        at=r.started_at, symbol=r.symbol, buy_exchange=r.buy_exchange, sell_exchange=r.sell_exchange, outcome=r.outcome,
        notional_usdt=notional,
        predicted_net_usd=float(r.predicted_net_profit_usd) if r.predicted_net_profit_usd is not None else None,
        actual_net_usd=float(r.actual_net_pnl_usd) if r.actual_net_pnl_usd is not None else None,
        buy_filled_qty=float(r.buy_filled_qty), buy_avg_price=float(r.buy_avg_fill_price) if r.buy_avg_fill_price is not None else None,
        sell_filled_qty=float(r.sell_filled_qty), sell_avg_price=float(r.sell_avg_fill_price) if r.sell_avg_fill_price is not None else None,
        total_fees_usd=total_fees, latency_ms=latency,
    )


def compute_real_pnl_breakdown(
    arb_rows: list[LiveArbitrageExecutionRecord], now: datetime, today_start: datetime,
    session_start: datetime | None = None, last_n: int = 20,
) -> tuple[RealPnlBreakdown, list[LiveTradeRow]]:
    """Pure computation. Returns (breakdown, last_n_trade_rows_desc)."""
    complete = [r for r in arb_rows if r.outcome == "both_filled"]
    trade_rows = [_to_trade_row(r) for r in complete]

    pnls = [t.actual_net_usd for t in trade_rows if t.actual_net_usd is not None]
    wins = sum(1 for p in pnls if p > 0)
    win_rate = (wins / len(pnls) * 100) if pnls else None
    avg_pnl = statistics.fmean(pnls) if pnls else None

    today_pnl = sum(t.actual_net_usd or 0.0 for t in trade_rows if t.at is not None and t.at >= today_start)
    total_pnl = sum(pnls)

    session_pnl = None
    pnl_per_hour = None
    if session_start is not None:
        session_trades = [t for t in trade_rows if t.at is not None and t.at >= session_start]
        session_pnl = sum(t.actual_net_usd or 0.0 for t in session_trades)
        elapsed_hours = max((now - session_start).total_seconds() / 3600, 1e-9)
        pnl_per_hour = session_pnl / elapsed_hours

    predicted = [t.predicted_net_usd for t in trade_rows if t.predicted_net_usd is not None]
    predicted_total = sum(predicted)
    prediction_errors = [
        (t.actual_net_usd - t.predicted_net_usd) for t in trade_rows if t.actual_net_usd is not None and t.predicted_net_usd is not None
    ]
    avg_pred_error = statistics.fmean(prediction_errors) if prediction_errors else None
    max_pred_error = max((abs(e) for e in prediction_errors), default=None)

    best = max(trade_rows, key=lambda t: t.actual_net_usd if t.actual_net_usd is not None else float("-inf"), default=None)
    worst = min(trade_rows, key=lambda t: t.actual_net_usd if t.actual_net_usd is not None else float("inf"), default=None)
    if best is not None and best.actual_net_usd is None:
        best = None
    if worst is not None and worst.actual_net_usd is None:
        worst = None

    breakdown = RealPnlBreakdown(
        session_pnl_usd=session_pnl, today_pnl_usd=today_pnl, total_pnl_usd=total_pnl, pnl_per_hour_usd=pnl_per_hour,
        average_pnl_per_trade_usd=avg_pnl, best_trade=best, worst_trade=worst, win_rate_pct=win_rate,
        predicted_total_pnl_usd=predicted_total, actual_total_pnl_usd=total_pnl, prediction_error_usd=total_pnl - predicted_total,
        average_prediction_error_usd=avg_pred_error, max_prediction_error_usd=max_pred_error,
    )
    last_trades = sorted(trade_rows, key=lambda t: t.at, reverse=True)[:last_n]
    return breakdown, last_trades


def compute_inventory_constitution_summary(inv_rows: list[InventoryConstitutionRecord]) -> InventoryConstitutionSummary:
    filled = [r for r in inv_rows if r.outcome == "filled"]
    seen_pairs: set[tuple[str, str]] = set()
    new_count = 0
    recycling_count = 0
    for r in sorted(filled, key=lambda r: r.started_at):
        key = (r.symbol, r.sell_exchange)
        if key in seen_pairs:
            recycling_count += 1
        else:
            seen_pairs.add(key)
            new_count += 1
    total_cost = sum(float(r.fee_amount) for r in filled if r.fee_asset == "USDT" and r.fee_amount is not None)
    return InventoryConstitutionSummary(
        total_constitutions=len(filled), new_constitutions=new_count, recycling_constitutions=recycling_count, total_inventory_cost_usd=total_cost,
    )


def compute_inventory_position_status(value_usdt: float | None, min_notional: float = 5.0) -> str:
    if value_usdt is None:
        return "UNKNOWN"
    if value_usdt <= 0:
        return "DUST"
    if value_usdt < min_notional:
        return "LOW"
    return "READY"


def compute_cost_basis_by_asset_exchange(
    arb_rows: list[LiveArbitrageExecutionRecord], inv_rows: list[InventoryConstitutionRecord],
) -> dict[tuple[str, str], float]:
    """Pure. Weighted-average price paid per unit, keyed by (base_asset,
    exchange) -- see module docstring for the FIFO/LIFO caveat."""
    qty_by_key: dict[tuple[str, str], float] = {}
    cost_by_key: dict[tuple[str, str], float] = {}

    def _add(key: tuple[str, str], qty: float, price: float) -> None:
        if qty <= 0 or price is None or price <= 0:
            return
        qty_by_key[key] = qty_by_key.get(key, 0.0) + qty
        cost_by_key[key] = cost_by_key.get(key, 0.0) + qty * price

    for r in arb_rows:
        if r.outcome != "both_filled" or r.buy_avg_fill_price is None:
            continue
        base_asset = r.symbol.removesuffix("USDT")
        qty = float(r.buy_net_filled_qty) if r.buy_net_filled_qty is not None else float(r.buy_filled_qty)
        _add((base_asset, r.buy_exchange), qty, float(r.buy_avg_fill_price))

    for r in inv_rows:
        if r.outcome != "filled" or r.avg_fill_price is None:
            continue
        base_asset = r.symbol.removesuffix("USDT")
        qty = float(r.net_filled_qty) if r.net_filled_qty is not None else float(r.filled_qty)
        _add((base_asset, r.sell_exchange), qty, float(r.avg_fill_price))

    return {key: (cost_by_key[key] / qty_by_key[key]) for key in qty_by_key if qty_by_key[key] > 0}


FUNNEL_CAUSE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "MIN_NOTIONAL": ("min_notional", "below min"),
    "INVENTORY": ("inventory", "no common quantity"),
    "SAFETY_MARGIN": ("safety_margin", "safety margin"),
    "EDGE_DISAPPEARED": ("edge", "not net-positive", "net profit"),
    "DEPTH": ("depth", "insufficient depth", "tradability"),
    "FEES": ("fee",),
}


@dataclass(slots=True)
class MissedOpportunityCause:
    cause: str
    count: int


def compute_missed_opportunity_causes(arb_rows: list[LiveArbitrageExecutionRecord], inv_rows: list[InventoryConstitutionRecord]) -> list[MissedOpportunityCause]:
    """Pure, best-effort. Neither ledger persists a structured rejection
    reason -- only the free-text `reason` string each executor already
    logs. Buckets that text by keyword; anything unrecognized falls into
    OTHER. This is real data from real attempts, but coarser than a
    proper structured funnel (see module docstring, limitation 1)."""
    counts: dict[str, int] = {}
    for r in list(arb_rows) + list(inv_rows):
        if r.outcome in ("both_filled", "filled"):
            continue
        reason = (r.reason or "").lower()
        if not reason:
            continue
        matched = False
        for cause, keywords in FUNNEL_CAUSE_KEYWORDS.items():
            if any(kw in reason for kw in keywords):
                counts[cause] = counts.get(cause, 0) + 1
                matched = True
                break
        if not matched:
            counts["OTHER"] = counts.get("OTHER", 0) + 1
    return sorted((MissedOpportunityCause(cause=c, count=n) for c, n in counts.items()), key=lambda m: m.count, reverse=True)


async def build_live_ledger_rows(session: AsyncSession) -> tuple[list[LiveArbitrageExecutionRecord], list[InventoryConstitutionRecord]]:
    arb_stmt = select(LiveArbitrageExecutionRecord).order_by(LiveArbitrageExecutionRecord.started_at)
    inv_stmt = select(InventoryConstitutionRecord).order_by(InventoryConstitutionRecord.started_at)
    arb_rows = list((await session.execute(arb_stmt)).scalars().all())
    inv_rows = list((await session.execute(inv_stmt)).scalars().all())
    return arb_rows, inv_rows
