"""Execution Engine Audit — Funnel (pre-live-trading audit, user request).

Six stages an opportunity actually passes through, each a strict subset of
the one before it:

  1. detected             — a distinct economic event was recorded
                             (Continuous Execution spec's own dedup: one
                             OpportunityRecord row per signal, continuations
                             update it rather than inserting duplicates)
  2. profitable_after_fees — net_spread_pct > 0: the edge survives fees,
                             VWAP fill cost, and detection-time liquidity —
                             before any other gate
  3. rejected              — did not clear app.execution.validator.validate()
                             for any reason (rejection_reason is set)
  4. executable            — cleared validate() (rejection_reason is None) —
                             capital allocation would have been attempted
  5. executed              — at least one portfolio actually booked a paper
                             trade against it (distinct opportunities, not
                             raw trade rows — 5 portfolios can each trade
                             the same opportunity, which would otherwise
                             inflate this stage 5x relative to the others)
  6. closed                — that trade's holding period has elapsed; a
                             final, booked result exists (not still open)

Every stage's percentage is of the "detected" total — the standard funnel
convention, so a glance shows exactly how much falls away at each step
rather than requiring mental multiplication across conversion rates.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES


@dataclass(slots=True)
class FunnelStage:
    name: str
    count: int
    pct_of_detected: float


@dataclass(slots=True)
class ExecutionFunnelReport:
    stages: list[FunnelStage]
    rejection_reasons: list[tuple[str, int, float]] = field(default_factory=list)  # (reason, count, pct_of_rejected)

    def stage(self, name: str) -> FunnelStage | None:
        return next((s for s in self.stages if s.name == name), None)


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def build_execution_funnel_report(
    *,
    detected: int,
    profitable_after_fees: int,
    rejected: int,
    executable: int,
    executed: int,
    closed: int,
    rejection_counts: list[tuple[str, int]],
) -> ExecutionFunnelReport:
    """Pure aggregation step — every input is an already-fetched count, so
    this is unit-testable without a database."""
    stages = [
        FunnelStage("detected", detected, 100.0 if detected else 0.0),
        FunnelStage("profitable_after_fees", profitable_after_fees, _pct(profitable_after_fees, detected)),
        FunnelStage("rejected", rejected, _pct(rejected, detected)),
        FunnelStage("executable", executable, _pct(executable, detected)),
        FunnelStage("executed", executed, _pct(executed, detected)),
        FunnelStage("closed", closed, _pct(closed, detected)),
    ]
    total_rejected = sum(count for _, count in rejection_counts)
    rejection_reasons = [(reason, count, _pct(count, total_rejected)) for reason, count in rejection_counts]
    return ExecutionFunnelReport(stages=stages, rejection_reasons=rejection_reasons)


async def build_execution_funnel(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> ExecutionFunnelReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    detected = (
        await session.execute(select(func.count()).where(OpportunityRecord.detected_at >= cutoff))
    ).scalar() or 0

    profitable_after_fees = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.detected_at >= cutoff, OpportunityRecord.net_spread_pct > 0)
        )
    ).scalar() or 0

    rejected = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.detected_at >= cutoff, OpportunityRecord.rejection_reason.is_not(None))
        )
    ).scalar() or 0

    executable = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.detected_at >= cutoff, OpportunityRecord.rejection_reason.is_(None))
        )
    ).scalar() or 0

    rejection_rows = (
        await session.execute(
            select(OpportunityRecord.rejection_reason, func.count())
            .where(OpportunityRecord.detected_at >= cutoff, OpportunityRecord.rejection_reason.is_not(None))
            .group_by(OpportunityRecord.rejection_reason)
            .order_by(func.count().desc())
        )
    ).all()

    executed = (
        await session.execute(
            select(func.count(func.distinct(SimulatedTradeRecord.opportunity_id)))
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(OpportunityRecord.detected_at >= cutoff, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES))
        )
    ).scalar() or 0

    executed_rows = (
        await session.execute(
            select(SimulatedTradeRecord.opportunity_id, SimulatedTradeRecord.executed_at, OpportunityRecord.holding_period_seconds)
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(OpportunityRecord.detected_at >= cutoff, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES))
        )
    ).all()
    closed_opportunity_ids = {
        opp_id
        for opp_id, executed_at, holding_period_seconds in executed_rows
        if holding_period_seconds is not None and executed_at + timedelta(seconds=float(holding_period_seconds)) <= now
    }
    closed = len(closed_opportunity_ids)

    return build_execution_funnel_report(
        detected=int(detected),
        profitable_after_fees=int(profitable_after_fees),
        rejected=int(rejected),
        executable=int(executable),
        executed=int(executed),
        closed=int(closed),
        rejection_counts=[(reason, int(count)) for reason, count in rejection_rows],
    )
