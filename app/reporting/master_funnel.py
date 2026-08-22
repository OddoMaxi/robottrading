"""Master Funnel + Frequency KPI (Multi-Market Opportunity Engine, V5.5,
spec sections 26, 36).

Section 37's own framing: "We want to increase REALISTIC EXECUTABLE
OPPORTUNITIES / HOUR, not RAW SIGNALS / HOUR." Both funnels this module
builds report executable counts (rejection_reason IS NULL — cleared every
gate a strategy applies) per strategy AND combined across every strategy
in the shared Opportunity Bus (CEX and DEX alike, spec section 1) — the
"Master Executable Opportunities" / "best opportunities selected" section
26 asks for is exactly the combined row here, since everything already
lives in one opportunities table.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES


@dataclass(slots=True)
class StrategyFrequency:
    strategy: str
    detected_count: int
    executable_count: int
    executed_count: int  # distinct opportunities with >=1 EXECUTED_STATUSES trade
    detected_per_hour: float
    executable_per_hour: float
    executed_per_hour: float


@dataclass(slots=True)
class MasterFrequencyReport:
    hours: float
    by_strategy: list[StrategyFrequency]
    total_detected: int
    total_executable: int
    total_executed: int
    total_detected_per_hour: float
    total_executable_per_hour: float
    total_executed_per_hour: float


async def build_master_frequency_report(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> MasterFrequencyReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)
    executable_case = case((OpportunityRecord.rejection_reason.is_(None), 1), else_=0)

    per_strategy_rows = (
        await session.execute(
            select(
                OpportunityRecord.strategy,
                func.count(),
                func.coalesce(func.sum(executable_case), 0),
            )
            .where(OpportunityRecord.detected_at >= cutoff)
            .group_by(OpportunityRecord.strategy)
            .order_by(func.count().desc())
        )
    ).all()

    executed_rows = (
        await session.execute(
            select(OpportunityRecord.strategy, func.count(func.distinct(SimulatedTradeRecord.opportunity_id)))
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(OpportunityRecord.detected_at >= cutoff, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES))
            .group_by(OpportunityRecord.strategy)
        )
    ).all()
    executed_by_strategy = {strategy: int(count) for strategy, count in executed_rows}

    by_strategy = []
    total_detected = total_executable = total_executed = 0
    for strategy, detected, executable in per_strategy_rows:
        detected = int(detected)
        executable = int(executable)
        executed = executed_by_strategy.get(strategy, 0)
        total_detected += detected
        total_executable += executable
        total_executed += executed
        by_strategy.append(
            StrategyFrequency(
                strategy=strategy,
                detected_count=detected,
                executable_count=executable,
                executed_count=executed,
                detected_per_hour=(detected / hours) if hours else 0.0,
                executable_per_hour=(executable / hours) if hours else 0.0,
                executed_per_hour=(executed / hours) if hours else 0.0,
            )
        )

    return MasterFrequencyReport(
        hours=hours,
        by_strategy=by_strategy,
        total_detected=total_detected,
        total_executable=total_executable,
        total_executed=total_executed,
        total_detected_per_hour=(total_detected / hours) if hours else 0.0,
        total_executable_per_hour=(total_executable / hours) if hours else 0.0,
        total_executed_per_hour=(total_executed / hours) if hours else 0.0,
    )
