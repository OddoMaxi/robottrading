"""Execution Engine Audit — Funnel (pre-live-trading audit, user request).

Seven stages an opportunity actually passes through, each a strict subset of
the one before it:

  1. detected             — a distinct economic event was recorded
                             (Continuous Execution spec's own dedup: one
                             OpportunityRecord row per signal, continuations
                             update it rather than inserting duplicates).
                             There is no separate "positive before costs"
                             count: every engine already discards a
                             non-positive gross spread before an Opportunity
                             object is ever constructed (app/engines/*), so
                             every detected row already has gross_spread_pct
                             > 0 by construction — reporting that as a
                             distinct, smaller number than "detected" would
                             be fabricating a distinction the code doesn't
                             make.
  2. profitable_after_fees — net_spread_pct > 0: the edge survives fees,
                             VWAP fill cost, and detection-time liquidity —
                             before any other gate
  3. profitable            — classification is INTERESTING or better (a
                             strict subset of stage 2 — WATCH-classified
                             opportunities are net-positive but below the
                             minimum worth attempting; this is the same
                             boundary app.execution.validator.validate() uses
                             for its EDGE_TOO_LOW rejection)
  4. rejected              — did not clear app.execution.validator.validate()
                             for any reason (rejection_reason is set)
  5. executable            — cleared validate() (rejection_reason is None) —
                             capital allocation would have been attempted
  6. executed              — at least one portfolio actually booked a paper
                             trade against it (distinct opportunities, not
                             raw trade rows — 5 portfolios can each trade
                             the same opportunity, which would otherwise
                             inflate this stage 5x relative to the others)
  7. closed                — that trade's holding period has elapsed; a
                             final, booked result exists (not still open)

Every stage's percentage is of the "detected" total — the standard funnel
convention, so a glance shows exactly how much falls away at each step
rather than requiring mental multiplication across conversion rates.

Two more numbers live outside the stage list because they aren't subsets of
"detected" (they're trade-attempt granularity — up to 5x a single
opportunity, one per portfolio — so a "% of detected" would read past 100%):

  execution_attempts — every simulated_trades row (any status, all 5
                        portfolios) against an opportunity in this window.
  attempt_outcomes   — that same row set, grouped by its real TradeStatus.
                        This is what actually answers "the engine found
                        something and tried, but couldn't take it" —
                        NO_CAPITAL_AVAILABLE (this portfolio's capital was
                        already locked elsewhere) and MAX_CONCURRENT_POSITIONS
                        (this portfolio already held its concurrency limit)
                        are real, distinguishable outcomes today; there is
                        no equivalent opportunity-level rejection for either
                        one (an approved opportunity is always attempted
                        against all 5 portfolios regardless of any single
                        portfolio's capital state) — see app/simulation/paper_trader.py.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import OpportunityClassification
from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES

# The same "not worth attempting" boundary app.execution.validator.validate()
# uses for RejectionReason.EDGE_TOO_LOW — kept here as the single source of
# truth for what "profitable" (stage 3) means, rather than re-deriving it.
_UNPROFITABLE_CLASSIFICATIONS = (OpportunityClassification.NOT_PROFITABLE.value, OpportunityClassification.WATCH.value)


@dataclass(slots=True)
class FunnelStage:
    name: str
    count: int
    pct_of_detected: float


@dataclass(slots=True)
class ExecutionFunnelReport:
    stages: list[FunnelStage]
    rejection_reasons: list[tuple[str, int, float]] = field(default_factory=list)  # (reason, count, pct_of_rejected)
    # Raw scan-tick volume (sum of updates_count across every row) — always
    # >= "detected" (each row is observed >= 1 time), so it's reported
    # separately rather than folded into the stages list's own "% of
    # detected" convention, which would read oddly above 100%.
    observed: int = 0
    # Trade-attempt granularity (see module docstring) — also kept outside
    # the stages list for the same "would exceed 100% of detected" reason.
    execution_attempts: int = 0
    attempt_outcomes: list[tuple[str, int, float]] = field(default_factory=list)  # (status, count, pct_of_attempts)

    def stage(self, name: str) -> FunnelStage | None:
        return next((s for s in self.stages if s.name == name), None)


def _pct(count: int, total: int) -> float:
    return round(count / total * 100, 1) if total else 0.0


def build_execution_funnel_report(
    *,
    detected: int,
    profitable_after_fees: int,
    profitable: int,
    rejected: int,
    executable: int,
    executed: int,
    closed: int,
    rejection_counts: list[tuple[str, int]],
    observed: int = 0,
    execution_attempts: int = 0,
    attempt_outcome_counts: list[tuple[str, int]] | None = None,
) -> ExecutionFunnelReport:
    """Pure aggregation step — every input is an already-fetched count, so
    this is unit-testable without a database."""
    stages = [
        FunnelStage("detected", detected, 100.0 if detected else 0.0),
        FunnelStage("profitable_after_fees", profitable_after_fees, _pct(profitable_after_fees, detected)),
        FunnelStage("profitable", profitable, _pct(profitable, detected)),
        FunnelStage("rejected", rejected, _pct(rejected, detected)),
        FunnelStage("executable", executable, _pct(executable, detected)),
        FunnelStage("executed", executed, _pct(executed, detected)),
        FunnelStage("closed", closed, _pct(closed, detected)),
    ]
    total_rejected = sum(count for _, count in rejection_counts)
    rejection_reasons = [(reason, count, _pct(count, total_rejected)) for reason, count in rejection_counts]
    attempt_outcome_counts = attempt_outcome_counts or []
    attempt_outcomes = [(status, count, _pct(count, execution_attempts)) for status, count in attempt_outcome_counts]
    return ExecutionFunnelReport(
        stages=stages,
        rejection_reasons=rejection_reasons,
        observed=observed,
        execution_attempts=execution_attempts,
        attempt_outcomes=attempt_outcomes,
    )


async def build_execution_funnel(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> ExecutionFunnelReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    detected, observed = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(OpportunityRecord.updates_count), 0)).where(
                OpportunityRecord.detected_at >= cutoff
            )
        )
    ).first()
    detected = detected or 0
    observed = observed or 0

    profitable_after_fees = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.detected_at >= cutoff, OpportunityRecord.net_spread_pct > 0)
        )
    ).scalar() or 0

    profitable = (
        await session.execute(
            select(func.count()).where(
                OpportunityRecord.detected_at >= cutoff,
                OpportunityRecord.classification.is_not(None),
                OpportunityRecord.classification.not_in(_UNPROFITABLE_CLASSIFICATIONS),
            )
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

    attempt_rows = (
        await session.execute(
            select(SimulatedTradeRecord.status, func.count())
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(OpportunityRecord.detected_at >= cutoff)
            .group_by(SimulatedTradeRecord.status)
            .order_by(func.count().desc())
        )
    ).all()
    execution_attempts = sum(count for _, count in attempt_rows)

    return build_execution_funnel_report(
        detected=int(detected),
        profitable_after_fees=int(profitable_after_fees),
        profitable=int(profitable),
        rejected=int(rejected),
        executable=int(executable),
        executed=int(executed),
        closed=int(closed),
        rejection_counts=[(reason, int(count)) for reason, count in rejection_rows],
        observed=int(observed),
        execution_attempts=int(execution_attempts),
        attempt_outcome_counts=[(status, int(count)) for status, count in attempt_rows],
    )
