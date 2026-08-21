""""Why No Trade?" diagnostic (user request — capital-lock investigation).

Composes signals that already exist elsewhere (execution funnel, capital
utilization, open positions, robot health) into one live answer to "what
is the engine actually doing right now, and why hasn't it traded" for a
human staring at a quiet dashboard. Nothing here is a new judgment about
whether the silence is correct — it's the same audit trail used to
diagnose the 2026-08-20 legacy oversized basis positions (opened before
the % portfolio-based risk cap existed, still holding until 2026-09-25),
just packaged for a glance instead of a manual SQL session.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import PriceSnapshot, SimulatedTradeRecord
from app.reporting.execution_funnel import ExecutionFunnelReport, build_execution_funnel
from app.reporting.rotation import EXECUTED_STATUSES
from app.reporting.simple_summary import (
    CapitalUtilization,
    OpenPosition,
    RobotStatus,
    build_capital_utilization,
    build_robot_status,
    list_open_positions,
)


@dataclass(slots=True)
class WhyNoTradeReport:
    last_trade_at: datetime | None
    hours_since_last_trade: float | None
    market_events_since_last_trade: int | None
    funnel: ExecutionFunnelReport | None
    capital: CapitalUtilization
    open_positions: list[OpenPosition]
    robot_status: RobotStatus


async def build_why_no_trade_report(
    session: AsyncSession, portfolio_id: int, total_capital_usd: float, now: datetime | None = None
) -> WhyNoTradeReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    last_trade_at = (
        await session.execute(
            select(SimulatedTradeRecord.executed_at)
            .where(SimulatedTradeRecord.portfolio_id == portfolio_id, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES))
            .order_by(SimulatedTradeRecord.executed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()

    funnel = None
    hours_since = None
    market_events = None
    if last_trade_at is not None:
        # A floor above zero — a trade that just landed a moment ago still
        # gets a valid (tiny) funnel window rather than a division-by-zero
        # edge case in build_execution_funnel's own percentage math.
        hours_since = max((now - last_trade_at).total_seconds() / 3600.0, 1e-6)
        funnel = await build_execution_funnel(session, hours=hours_since, now=now)
        # Raw market ticks (every exchange, every symbol) since the last
        # trade — distinct from the funnel's "detected" count, which is
        # already deduplicated to one row per economic event. This answers
        # "was the engine even looking at fresh data" independent of
        # whether that data ever produced a tradeable signal.
        market_events = (
            await session.execute(select(func.count()).where(PriceSnapshot.recorded_at >= last_trade_at))
        ).scalar() or 0

    capital = await build_capital_utilization(session, portfolio_id, total_capital_usd, now=now)
    open_positions = await list_open_positions(session, portfolio_id, total_capital_usd, now=now)
    robot_status = await build_robot_status(session, now=now)

    return WhyNoTradeReport(
        last_trade_at=last_trade_at,
        hours_since_last_trade=hours_since,
        market_events_since_last_trade=market_events,
        funnel=funnel,
        capital=capital,
        open_positions=open_positions,
        robot_status=robot_status,
    )
