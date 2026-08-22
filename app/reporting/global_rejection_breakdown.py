"""Global Rejection Breakdown (V5/V5.5 Master Orchestration, user
directive, 2026-08-22, spec Part S).

Combines CEX's already-tracked attempt outcomes (app.reporting.
execution_funnel's rejection_reasons/attempt_outcomes) and DEX's
(app.reporting.dex_execution_funnel's per-status counts) into one
normalized list.

Several categories the spec lists — INSUFFICIENT_LIQUIDITY, GAS_TOO_HIGH,
SLIPPAGE_TOO_HIGH, MEV_RISK, CHAIN_RISK, VENUE_RISK, LATENCY — are NOT
separately tracked rejection reasons anywhere in this codebase today:
they're folded into the net edge calculation at detection time (an
opportunity whose price impact/gas/slippage/MEV cost pushes its net edge
below the minimum is simply never constructed as an Opportunity at all,
see app.onchain.cross_dex_arbitrage's own docstring), not recorded as a
distinct rejection event on a persisted attempt. Reporting a fabricated
count for these would violate the audit's own "never invent a missing
number" rule — they're listed here explicitly as NOT_SEPARATELY_TRACKED
rather than silently omitted or given a fake 0.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.reporting.dex_execution_funnel import DexStrategyFunnel, build_dex_execution_funnel
from app.reporting.execution_funnel import ExecutionFunnelReport, build_execution_funnel

NOT_SEPARATELY_TRACKED_REASONS = (
    "INSUFFICIENT_LIQUIDITY",
    "GAS_TOO_HIGH",
    "SLIPPAGE_TOO_HIGH",
    "MEV_RISK",
    "CHAIN_RISK",
    "VENUE_RISK",
    "LATENCY",
)


@dataclass(slots=True)
class RejectionReasonRow:
    reason: str
    engine: str  # "CEX", "DEX", or "BOTH" for a reason both engines record
    count: int
    tracked: bool = True


async def build_global_rejection_breakdown(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> list[RejectionReasonRow]:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    cex_funnel: ExecutionFunnelReport = await build_execution_funnel(session, hours=hours, now=now)
    dex_funnels: list[DexStrategyFunnel] = await build_dex_execution_funnel(session, hours=hours, now=now)

    rows: list[RejectionReasonRow] = []
    for reason, count, _pct in cex_funnel.rejection_reasons:
        rows.append(RejectionReasonRow(reason=reason.upper(), engine="CEX", count=count))
    for status, count, _pct in cex_funnel.attempt_outcomes:
        if status in ("simulated_executed", "partial_fill"):
            continue  # a fill, not a rejection
        rows.append(RejectionReasonRow(reason=status.upper(), engine="CEX", count=count))

    dex_duplicate_total = dex_no_capital_total = dex_edge_disappeared_total = dex_not_profitable_total = dex_failed_total = 0
    for f in dex_funnels:
        dex_duplicate_total += f.duplicate_economic_event
        dex_no_capital_total += f.no_capital_available
        dex_edge_disappeared_total += f.edge_disappeared
        dex_not_profitable_total += f.not_profitable_at_size
        dex_failed_total += f.failed
    if dex_duplicate_total:
        rows.append(RejectionReasonRow(reason="DUPLICATE_ECONOMIC_EVENT", engine="DEX", count=dex_duplicate_total))
    if dex_no_capital_total:
        rows.append(RejectionReasonRow(reason="NO_CAPITAL_AVAILABLE", engine="DEX", count=dex_no_capital_total))
    if dex_edge_disappeared_total:
        rows.append(RejectionReasonRow(reason="EDGE_DISAPPEARED", engine="DEX", count=dex_edge_disappeared_total))
    if dex_not_profitable_total:
        rows.append(RejectionReasonRow(reason="NOT_PROFITABLE_AT_SIZE", engine="DEX", count=dex_not_profitable_total))
    if dex_failed_total:
        rows.append(RejectionReasonRow(reason="FAILED", engine="DEX", count=dex_failed_total))

    for reason in NOT_SEPARATELY_TRACKED_REASONS:
        rows.append(RejectionReasonRow(reason=reason, engine="BOTH", count=0, tracked=False))

    return sorted((r for r in rows if r.tracked), key=lambda r: r.count, reverse=True) + [r for r in rows if not r.tracked]
