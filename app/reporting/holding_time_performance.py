"""Performance-by-Holding-Time-Bucket reporting (Fast-Rotation spec, section 45).

Breaks a portfolio's simulated trade history down by HoldingTimeCategory
(ultra_fast/fast/medium/carry) instead of lumping everything together —
answers which time horizon is actually carrying the portfolio's P&L, which
raw "total profit" can hide (a handful of big Carry wins can mask a Fast
book that loses on fees more often than it wins).
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES


@dataclass(slots=True)
class HoldingTimeBucketStats:
    holding_time_category: str
    trade_count: int
    win_count: int
    win_rate_pct: float
    net_pnl_usd: float
    avg_net_profit_per_trade_usd: float
    avg_holding_time_seconds: float | None


async def build_holding_time_performance(
    session: AsyncSession,
    portfolio_id: int,
    hours: float = 24.0,
    now: datetime | None = None,
) -> list[HoldingTimeBucketStats]:
    """One row per holding-time bucket actually traded in the window,
    ranked by net P&L (highest first) so the best-performing horizon
    always sorts to the top of the dashboard table."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    period_start = now - timedelta(hours=hours)

    win_case = case((SimulatedTradeRecord.net_profit_usd > 0, 1), else_=0)
    net_pnl_sum = func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0)
    query = (
        select(
            OpportunityRecord.holding_time_category,
            func.count(),
            func.coalesce(func.sum(win_case), 0),
            net_pnl_sum,
            func.avg(OpportunityRecord.holding_period_seconds),
        )
        .select_from(SimulatedTradeRecord)
        .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
        .where(
            SimulatedTradeRecord.portfolio_id == portfolio_id,
            SimulatedTradeRecord.executed_at >= period_start,
            SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
            OpportunityRecord.holding_time_category.is_not(None),
        )
        .group_by(OpportunityRecord.holding_time_category)
        .order_by(net_pnl_sum.desc())
    )
    rows = (await session.execute(query)).all()

    results = []
    for category, count, wins, net_pnl, avg_holding in rows:
        net_pnl = float(net_pnl)
        wins = int(wins)
        results.append(
            HoldingTimeBucketStats(
                holding_time_category=category,
                trade_count=count,
                win_count=wins,
                win_rate_pct=(wins / count * 100) if count else 0.0,
                net_pnl_usd=net_pnl,
                avg_net_profit_per_trade_usd=(net_pnl / count) if count else 0.0,
                avg_holding_time_seconds=float(avg_holding) if avg_holding is not None else None,
            )
        )
    return results
