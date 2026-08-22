"""Duplicate Monitor (V5/V5.5 Master Orchestration, user directive,
2026-08-22, spec Part T).

Surfaces the reality audit's own confirmed finding (main.py's
duplicate_economic_event fix, deployed as this codebase's
app.reporting.reality_baseline.REALITY_BASELINE_AT): nearly every
cross-DEX/multi-hop opportunity also spawns an "atomic" sibling describing
the SAME real-world price gap. Only counts SINCE a baseline — before
REALITY_BASELINE_AT, duplicates were never marked at all (rejection_reason
was NULL on both twins).

PRE-PHASE-2 CORRECTIVE MAINTENANCE (2026-08-22): defaults to
PRE_PHASE_2_VALIDATION_BASELINE_AT when that restart has happened — the
window between REALITY_BASELINE_AT and that later baseline has a
CONFIRMED counter undercount (~50%, see reality_baseline's own docstring
for the full trace: update_opportunity_tracking was erasing the marking
on every continuation) and is reported as `reliable=False` / a `LEGACY /
NOT RELIABLE` note rather than silently presented as accurate. The
underlying capital-safety guarantee (no double execution) is unaffected
and was independently verified for that whole window by direct
pool/timestamp matching — only the persisted COUNTER was wrong.

"simulated fake P&L prevented" is reported as an ESTIMATE derived from the
duplicate's own already-computed expected_profit_usd (a real, persisted
field — never a fabricated number), explicitly labeled as an estimate
since the duplicate was never actually attempted (spec Part T's own rule:
"If it cannot be reliably reconstructed: N/A" — expected_profit_usd IS
reconstructable, so this is not N/A, but it is a projection, not an
observed fill outcome, and is labeled as such).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord
from app.reporting.reality_baseline import LEGACY_DUPLICATE_COUNT_WINDOW_NOTE, PRE_PHASE_2_VALIDATION_BASELINE_AT, REALITY_BASELINE_AT

DEX_ATTEMPTABLE_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic")


@dataclass(slots=True)
class DuplicateMonitorReport:
    since: datetime
    raw_detections: int
    duplicate_economic_events_eliminated: int
    unique_economic_opportunities: int
    estimated_fake_pnl_prevented_usd: float | None  # None only if there's nothing to estimate from
    reliable: bool  # False if `since` predates PRE_PHASE_2_VALIDATION_BASELINE_AT — see module docstring
    legacy_note: str | None  # explanation when reliable=False, else None


async def build_duplicate_monitor_report(session: AsyncSession, now: datetime | None = None, since: datetime | None = None) -> DuplicateMonitorReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    if since is None:
        since = PRE_PHASE_2_VALIDATION_BASELINE_AT or REALITY_BASELINE_AT
    reliable = PRE_PHASE_2_VALIDATION_BASELINE_AT is not None and since >= PRE_PHASE_2_VALIDATION_BASELINE_AT

    raw_row = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.strategy.in_(DEX_ATTEMPTABLE_STRATEGIES), OpportunityRecord.detected_at >= since)
        )
    ).scalar() or 0
    raw_detections = int(raw_row)

    dup_row = (
        await session.execute(
            select(func.count(), func.coalesce(func.sum(OpportunityRecord.expected_profit_usd), 0)).where(
                OpportunityRecord.strategy.in_(DEX_ATTEMPTABLE_STRATEGIES),
                OpportunityRecord.detected_at >= since,
                OpportunityRecord.rejection_reason == "duplicate_economic_event",
            )
        )
    ).first()
    duplicates_eliminated = int(dup_row[0] or 0)
    estimated_prevented = float(dup_row[1]) if duplicates_eliminated else None

    return DuplicateMonitorReport(
        since=since,
        raw_detections=raw_detections,
        duplicate_economic_events_eliminated=duplicates_eliminated,
        unique_economic_opportunities=raw_detections - duplicates_eliminated,
        estimated_fake_pnl_prevented_usd=estimated_prevented,
        reliable=reliable,
        legacy_note=None if reliable else LEGACY_DUPLICATE_COUNT_WINDOW_NOTE,
    )
