"""Reality Reliability (V5/V5.5 Master Orchestration, user directive,
2026-08-22, spec Part AF).

"If the system can objectively calculate simulation reliability using
existing measurable criteria, expose it... DO NOT invent an arbitrary
confidence score. If not objectively defensible: show N/A and explain
why." No single composite reliability score is implemented here — there
is no principled way to weight "40% data completeness + 30% replay
coverage + 30% failure rate" into one number without an arbitrary
judgment call this module refuses to fabricate. Instead, this exposes the
individual, genuinely measurable sub-metrics and leaves the composite
explicitly N/A.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DexSimulatedTradeRecord, OpportunityRecord
from app.reporting.reality_baseline import REALITY_BASELINE_AT

DEX_CROSS_STRATEGY = "dex_cross"
DEX_ATTEMPTABLE_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic")


@dataclass(slots=True)
class RealityReliabilityReport:
    since: datetime
    replay_coverage_pct: float | None  # dex_cross opportunities with a full price/tvl/fee leg snapshot (independently recomputable) / all dex_cross opportunities
    replay_coverage_note: str
    dex_data_completeness_pct: float | None  # DEX attempts with every timestamp field populated / all DEX attempts
    dex_attempt_failure_rate_pct: float | None  # dex_failed / total attempts (a genuine, measured outcome rate, not a confidence score)
    composite_score: None  # explicitly never computed — see module docstring
    composite_score_reason: str


async def build_reality_reliability_report(session: AsyncSession, now: datetime | None = None) -> RealityReliabilityReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    since = REALITY_BASELINE_AT

    dex_cross_total = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.strategy == DEX_CROSS_STRATEGY, OpportunityRecord.detected_at >= since)
        )
    ).scalar() or 0

    replay_coverage_pct = None
    if dex_cross_total:
        rows = (
            await session.execute(
                select(OpportunityRecord.legs).where(OpportunityRecord.strategy == DEX_CROSS_STRATEGY, OpportunityRecord.detected_at >= since)
            )
        ).scalars().all()
        with_snapshot = sum(1 for legs in rows if legs and len(legs) == 2 and all("price" in leg and "tvl_usd" in leg for leg in legs))
        replay_coverage_pct = round(with_snapshot / dex_cross_total * 100, 1)

    total_attempts = (
        await session.execute(select(func.count()).where(DexSimulatedTradeRecord.execution_complete_at >= since))
    ).scalar() or 0

    dex_data_completeness_pct = None
    dex_attempt_failure_rate_pct = None
    if total_attempts:
        complete = (
            await session.execute(
                select(func.count()).where(
                    DexSimulatedTradeRecord.execution_complete_at >= since,
                    DexSimulatedTradeRecord.detection_at.is_not(None),
                    DexSimulatedTradeRecord.validation_at.is_not(None),
                    DexSimulatedTradeRecord.execution_attempt_at.is_not(None),
                    DexSimulatedTradeRecord.execution_complete_at.is_not(None),
                )
            )
        ).scalar() or 0
        dex_data_completeness_pct = round(complete / total_attempts * 100, 1)

        failed = (
            await session.execute(
                select(func.count()).where(
                    DexSimulatedTradeRecord.execution_complete_at >= since, DexSimulatedTradeRecord.status == "dex_failed"
                )
            )
        ).scalar() or 0
        dex_attempt_failure_rate_pct = round(failed / total_attempts * 100, 1)

    return RealityReliabilityReport(
        since=since,
        replay_coverage_pct=replay_coverage_pct,
        replay_coverage_note="dex_cross only — dex_triangular/dex_multihop/atomic have no independent replay recomputation yet (see app.reporting.dex_replay's own disclosed scope limit)",
        dex_data_completeness_pct=dex_data_completeness_pct,
        dex_attempt_failure_rate_pct=dex_attempt_failure_rate_pct,
        composite_score=None,
        composite_score_reason="No principled way to weight these sub-metrics into one confidence number without an arbitrary judgment call — showing the individual measurements instead of fabricating a composite (spec Part AF's own rule).",
    )
