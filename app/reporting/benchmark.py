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
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES

CEX_STRATEGIES = ("stablecoin", "cross_exchange", "triangular", "funding", "basis")
DEX_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic", "flash_loan_research")


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


async def _build_engine_benchmark(session: AsyncSession, label: str, strategies: tuple[str, ...], cutoff: datetime, hours: float) -> EngineBenchmark:
    executable_case = case((OpportunityRecord.rejection_reason.is_(None), 1), else_=0)
    detected_row = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(executable_case), 0)).where(
                OpportunityRecord.detected_at >= cutoff, OpportunityRecord.strategy.in_(strategies)
            )
        )
    ).first()
    unique_opportunities, executable_opportunities = int(detected_row[0] or 0), int(detected_row[1] or 0)

    executed_row = (
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
    executed_opportunities = int(executed_row[0] or 0)
    net_pnl_usd = float(executed_row[1] or 0.0)
    avg_holding_seconds = float(executed_row[2]) if executed_row[2] is not None else None

    return EngineBenchmark(
        label=label,
        hours=hours,
        unique_opportunities=unique_opportunities,
        executable_opportunities=executable_opportunities,
        executed_opportunities=executed_opportunities,
        unique_opportunities_per_hour=(unique_opportunities / hours) if hours else 0.0,
        executable_per_hour=(executable_opportunities / hours) if hours else 0.0,
        net_pnl_usd=net_pnl_usd,
        avg_holding_seconds=avg_holding_seconds,
    )


async def build_benchmark_report(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> BenchmarkReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    cex_only = await _build_engine_benchmark(session, "CEX seul", CEX_STRATEGIES, cutoff, hours)
    dex_only = await _build_engine_benchmark(session, "DEX seul (V5.5)", DEX_STRATEGIES, cutoff, hours)
    combined = await _build_engine_benchmark(session, "Multi-Market (CEX + DEX)", CEX_STRATEGIES + DEX_STRATEGIES, cutoff, hours)

    uplift_pct = None
    if cex_only.executable_per_hour > 0:
        uplift_pct = round((combined.executable_per_hour - cex_only.executable_per_hour) / cex_only.executable_per_hour * 100, 1)

    return BenchmarkReport(cex_only=cex_only, combined=combined, dex_only=dex_only, executable_per_hour_uplift_pct=uplift_pct)
