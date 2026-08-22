"""DEX Reality Capture (Multi-Market Opportunity Engine, V5.5, spec section 22).

Same philosophy as app.reporting.simple_summary's CEX Reality Capture
Ratio ("how much of the spread the robot sees at detection actually
survives a realistic simulated fill, on average"), adapted to what DEX
actually has: there's no separate paper-trading fill-simulation layer for
DEX yet (no on-chain "shadow execution outcome" is booked anywhere — spec
section 23's shadow mode records what the engine WOULD have attempted,
not a simulated fill result), so "realistically simulated profit" here is
the realistic_executable_edge_pct app.onchain.cross_dex_arbitrage/
multihop_arbitrage/atomic_arbitrage/flash_loan_research already compute —
the edge AFTER swap fees, gas, AMM price impact, slippage buffer, and MEV
buffer, at the optimal size. Potential = theoretical_edge_pct (raw,
size-blind top-of-book spread). The ratio between them answers exactly the
same question CEX's Reality Capture Ratio does: how much of what looks
good at first glance actually survives contact with real execution costs.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord

DEX_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic", "flash_loan_research")


@dataclass(slots=True)
class DexRealityCaptureReport:
    strategy: str | None  # None = all DEX strategies combined
    opportunity_count: int
    avg_theoretical_edge_pct: float | None
    avg_realistic_executable_edge_pct: float | None
    capture_ratio_pct: float | None  # avg_realistic / avg_theoretical * 100


def _build_report(strategy: str | None, count: int, avg_theoretical, avg_realistic) -> DexRealityCaptureReport:
    avg_theoretical = float(avg_theoretical) if avg_theoretical is not None else None
    avg_realistic = float(avg_realistic) if avg_realistic is not None else None
    capture_ratio_pct = None
    if avg_theoretical is not None and avg_theoretical > 0 and avg_realistic is not None:
        capture_ratio_pct = round(avg_realistic / avg_theoretical * 100, 1)
    return DexRealityCaptureReport(
        strategy=strategy,
        opportunity_count=count,
        avg_theoretical_edge_pct=avg_theoretical,
        avg_realistic_executable_edge_pct=avg_realistic,
        capture_ratio_pct=capture_ratio_pct,
    )


async def build_dex_reality_capture(
    session: AsyncSession, hours: float = 24.0, now: datetime | None = None
) -> list[DexRealityCaptureReport]:
    """One report for each DEX strategy that had at least one detected
    opportunity in the window, plus a combined "all DEX strategies" report
    — same per-strategy + aggregate shape
    app.reporting.rotation.build_rotation_report already uses for CEX."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    base_filter = (
        OpportunityRecord.detected_at >= cutoff,
        OpportunityRecord.strategy.in_(DEX_STRATEGIES),
        OpportunityRecord.theoretical_edge_pct.is_not(None),
        OpportunityRecord.realistic_executable_edge_pct.is_not(None),
    )

    combined_row = (
        await session.execute(
            select(
                func.count(),
                func.avg(OpportunityRecord.theoretical_edge_pct),
                func.avg(OpportunityRecord.realistic_executable_edge_pct),
            ).where(*base_filter)
        )
    ).first()
    reports = [_build_report(None, combined_row[0] or 0, combined_row[1], combined_row[2])]

    per_strategy_rows = (
        await session.execute(
            select(
                OpportunityRecord.strategy,
                func.count(),
                func.avg(OpportunityRecord.theoretical_edge_pct),
                func.avg(OpportunityRecord.realistic_executable_edge_pct),
            )
            .where(*base_filter)
            .group_by(OpportunityRecord.strategy)
            .order_by(func.count().desc())
        )
    ).all()
    for strategy, count, avg_theoretical, avg_realistic in per_strategy_rows:
        reports.append(_build_report(strategy, count, avg_theoretical, avg_realistic))

    return reports
