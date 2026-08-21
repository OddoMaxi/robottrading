"""Shadow Live status (Reality Engine spec, sections 55-56).

Confirms, in one auditable place, that the running engine is a genuine
shadow of live trading: real market data feeding the detection pipeline
(RobotStatus), decisions computed by the real detect/validate/paper-trade
path for every signal currently on the radar, and zero real orders ever
placed — true by construction (no order-placement code path exists
anywhere in this codebase), reported explicitly here rather than left as
an implicit assumption someone has to trust.

Deliberately queries *currently open* signals (status in DETECTED/ACTIVE/
OPEN) rather than a time-windowed "last N hours" — OpportunityRecord.detected_at
is set once at row creation and never touched by continuation updates, so a
detected_at filter would silently miss a signal that's been continuously
observed for an hour but first appeared just outside the window. Asking
"what is the engine looking at right now" is both the more honest question
and the simpler query.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import OpportunityStatus
from app.database.models import OpportunityRecord
from app.reporting.simple_summary import RobotStatus, build_robot_status
from app.risk.risk_engine import RiskEngine

_OPEN_STATUSES = (OpportunityStatus.DETECTED, OpportunityStatus.ACTIVE, OpportunityStatus.OPEN)


@dataclass(slots=True)
class ShadowLiveStatus:
    mode: str
    real_orders_placed: bool
    robot_status: RobotStatus
    kill_switch_engaged: bool
    kill_switch_reason: str | None
    signals_on_radar: int
    approved_on_radar: int
    rejection_breakdown: dict[str, int] = field(default_factory=dict)


def _aggregate_signal_counts(rows: list[tuple[str | None, int]]) -> tuple[int, int, dict[str, int]]:
    """Pure aggregation step behind build_shadow_live_status, split out so
    the approved/rejected-by-reason counting is unit-testable without a
    database. Returns (total, approved_count, rejection_breakdown)."""
    rejection_breakdown: dict[str, int] = {}
    approved_count = 0
    total = 0
    for reason, count in rows:
        total += count
        if reason is None:
            approved_count += count
        else:
            rejection_breakdown[reason] = count
    return total, approved_count, rejection_breakdown


async def build_shadow_live_status(session: AsyncSession, risk_engine: RiskEngine, now: datetime | None = None) -> ShadowLiveStatus:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    robot_status = await build_robot_status(session, now=now)

    rows = (
        await session.execute(
            select(OpportunityRecord.rejection_reason, func.count())
            .where(OpportunityRecord.status.in_([s.value for s in _OPEN_STATUSES]))
            .group_by(OpportunityRecord.rejection_reason)
        )
    ).all()
    total, approved_count, rejection_breakdown = _aggregate_signal_counts(list(rows))

    return ShadowLiveStatus(
        mode="shadow_live",
        real_orders_placed=False,
        robot_status=robot_status,
        kill_switch_engaged=risk_engine.kill_switch_engaged,
        kill_switch_reason=risk_engine.kill_switch_reason,
        signals_on_radar=total,
        approved_on_radar=approved_count,
        rejection_breakdown=rejection_breakdown,
    )
