"""Benchmark: CEX-only vs Multi-Market Engine (Multi-Market Opportunity
Engine, V5.5, spec section 38).

"Before and after implementation compare: Current CEX-only engine vs
Multi-Market Engine." There is no "before" snapshot to replay (V5.5 was
built and deployed progressively over one session, not toggled on
wholesale at one instant) — so this compares CEX-only activity against
combined CEX+DEX activity within the SAME live window, which answers the
spec's own underlying question ("did adding DEX increase realistic
executable opportunities per hour") without fabricating a synthetic
"before" period that never actually ran standalone during this window.

REALITY AUDIT FIX (V5/V5.5 unification, user directive, 2026-08-22):
executed_opportunities/net_pnl_usd/avg_holding_seconds used to always join
SimulatedTradeRecord — the CEX-only ledger — even for dex_only and
combined. A DEX opportunity never has a SimulatedTradeRecord row (its
fills live in DexSimulatedTradeRecord, spec section 39's own isolation
guarantee), so dex_only.executed_opportunities/net_pnl_usd silently read
as 0/0.0 always, and combined's net P&L silently excluded every dollar of
DEX profit — found auditing this module while building the CEX-vs-DEX
Reality Dashboard comparison, before it could quietly understate the
"Multi-Market" benchmark card already live in the Expert dashboard. Now
queries each ledger separately and sums for "combined", never joining the
wrong table for a given strategy set.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DexSimulatedTradeRecord, OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES

CEX_STRATEGIES = ("stablecoin", "cross_exchange", "triangular", "funding", "basis")
DEX_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic", "flash_loan_research")
DEX_FILLED_STATUS = "dex_filled"


@dataclass(slots=True)
class EngineBenchmark:
    label: str
    hours: float
    unique_opportunities: int
    executable_opportunities: int
    executed_opportunities: int
    unique_opportunities_per_hour: float
    executable_per_hour: float
    net_pnl_usd: float
    avg_holding_seconds: float | None


@dataclass(slots=True)
class BenchmarkReport:
    cex_only: EngineBenchmark
    combined: EngineBenchmark
    dex_only: EngineBenchmark
    executable_per_hour_uplift_pct: float | None  # how much combined beats cex_only alone


async def _detected_and_executable(session: AsyncSession, strategies: tuple[str, ...], cutoff: datetime) -> tuple[int, int]:
    executable_case = case((OpportunityRecord.rejection_reason.is_(None), 1), else_=0)
    row = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(executable_case), 0)).where(
                OpportunityRecord.detected_at >= cutoff, OpportunityRecord.strategy.in_(strategies)
            )
        )
    ).first()
    return int(row[0] or 0), int(row[1] or 0)


async def _cex_executed(session: AsyncSession, strategies: tuple[str, ...], cutoff: datetime) -> tuple[int, float, float | None]:
    row = (
        await session.execute(
            select(
                func.count(func.distinct(SimulatedTradeRecord.opportunity_id)),
                func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0),
                func.avg(OpportunityRecord.holding_period_seconds),
            )
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(
                OpportunityRecord.detected_at >= cutoff,
                OpportunityRecord.strategy.in_(strategies),
                SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
            )
        )
    ).first()
    return int(row[0] or 0), float(row[1] or 0.0), (float(row[2]) if row[2] is not None else None)


async def _dex_executed(session: AsyncSession, strategies: tuple[str, ...], cutoff: datetime) -> tuple[int, float, float | None]:
    row = (
        await session.execute(
            select(
                func.count(func.distinct(DexSimulatedTradeRecord.opportunity_id)),
                func.coalesce(func.sum(DexSimulatedTradeRecord.net_profit_usd), 0),
                func.avg(DexSimulatedTradeRecord.validation_to_execution_ms),
            )
            .select_from(DexSimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == DexSimulatedTradeRecord.opportunity_id)
            .where(
                OpportunityRecord.detected_at >= cutoff,
                OpportunityRecord.strategy.in_(strategies),
                DexSimulatedTradeRecord.status == DEX_FILLED_STATUS,
            )
        )
    ).first()
    avg_holding_seconds = (float(row[2]) / 1000.0) if row[2] is not None else None
    return int(row[0] or 0), float(row[1] or 0.0), avg_holding_seconds


def _weighted_avg(a: float | None, a_n: int, b: float | None, b_n: int) -> float | None:
    if a is None and b is None:
        return None
    total_n = a_n + b_n
    if total_n == 0:
        return None
    return ((a or 0.0) * a_n + (b or 0.0) * b_n) / total_n


async def _build_engine_benchmark(
    session: AsyncSession, label: str, strategies: tuple[str, ...], cutoff: datetime, hours: float, include_cex: bool, include_dex: bool
) -> EngineBenchmark:
    unique_opportunities, executable_opportunities = await _detected_and_executable(session, strategies, cutoff)

    cex_n = cex_pnl = 0
    cex_hold = None
    if include_cex:
        cex_n, cex_pnl, cex_hold = await _cex_executed(session, strategies, cutoff)
    dex_n = dex_pnl = 0
    dex_hold = None
    if include_dex:
        dex_n, dex_pnl, dex_hold = await _dex_executed(session, strategies, cutoff)

    return EngineBenchmark(
        label=label,
        hours=hours,
        unique_opportunities=unique_opportunities,
        executable_opportunities=executable_opportunities,
        executed_opportunities=cex_n + dex_n,
        unique_opportunities_per_hour=(unique_opportunities / hours) if hours else 0.0,
        executable_per_hour=(executable_opportunities / hours) if hours else 0.0,
        net_pnl_usd=cex_pnl + dex_pnl,
        avg_holding_seconds=_weighted_avg(cex_hold, cex_n, dex_hold, dex_n),
    )


async def build_benchmark_report(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> BenchmarkReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    cex_only = await _build_engine_benchmark(session, "CEX seul", CEX_STRATEGIES, cutoff, hours, include_cex=True, include_dex=False)
    dex_only = await _build_engine_benchmark(session, "DEX seul (V5.5)", DEX_STRATEGIES, cutoff, hours, include_cex=False, include_dex=True)
    combined = await _build_engine_benchmark(
        session, "Multi-Market (CEX + DEX)", CEX_STRATEGIES + DEX_STRATEGIES, cutoff, hours, include_cex=True, include_dex=True
    )

    uplift_pct = None
    if cex_only.executable_per_hour > 0:
        uplift_pct = round((combined.executable_per_hour - cex_only.executable_per_hour) / cex_only.executable_per_hour * 100, 1)

    return BenchmarkReport(cex_only=cex_only, combined=combined, dex_only=dex_only, executable_per_hour_uplift_pct=uplift_pct)
