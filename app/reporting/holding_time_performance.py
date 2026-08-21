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


@dataclass(slots=True)
class HoldingTimeDistribution:
    """FAST TRADING ONLY (user directive, 2026-08-21) — is the engine
    actually behaving like a fast-rotation system, in one glance. Uses
    holding_period_seconds (the planned hold every strategy commits to at
    open time, which is what actually determines how long capital stays
    locked) — there is no separately-tracked "actual" duration in this V1
    model, positions don't run long or short of their planned hold."""

    trade_count: int
    avg_holding_seconds: float | None
    median_holding_seconds: float | None
    pct_under_5min: float
    pct_under_10min: float
    pct_under_20min: float
    longest_holding_seconds: float | None
    longest_trade_symbol: str | None
    longest_trade_executed_at: datetime | None


async def build_holding_time_distribution(
    session: AsyncSession, portfolio_id: int, hours: float = 24.0, now: datetime | None = None
) -> HoldingTimeDistribution:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    period_start = now - timedelta(hours=hours)

    under_5min = case((OpportunityRecord.holding_period_seconds < 300, 1), else_=0)
    under_10min = case((OpportunityRecord.holding_period_seconds < 600, 1), else_=0)
    under_20min = case((OpportunityRecord.holding_period_seconds < 1200, 1), else_=0)
    median_expr = func.percentile_cont(0.5).within_group(OpportunityRecord.holding_period_seconds)

    count, avg_holding, median_holding, under_5, under_10, under_20 = (
        await session.execute(
            select(
                func.count(),
                func.avg(OpportunityRecord.holding_period_seconds),
                median_expr,
                func.coalesce(func.sum(under_5min), 0),
                func.coalesce(func.sum(under_10min), 0),
                func.coalesce(func.sum(under_20min), 0),
            )
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(
                SimulatedTradeRecord.portfolio_id == portfolio_id,
                SimulatedTradeRecord.executed_at >= period_start,
                SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                OpportunityRecord.holding_period_seconds.is_not(None),
            )
        )
    ).first()
    count = count or 0

    longest_row = (
        await session.execute(
            select(OpportunityRecord.holding_period_seconds, OpportunityRecord.symbol, SimulatedTradeRecord.executed_at)
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(
                SimulatedTradeRecord.portfolio_id == portfolio_id,
                SimulatedTradeRecord.executed_at >= period_start,
                SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                OpportunityRecord.holding_period_seconds.is_not(None),
            )
            .order_by(OpportunityRecord.holding_period_seconds.desc())
            .limit(1)
        )
    ).first()

    return HoldingTimeDistribution(
        trade_count=count,
        avg_holding_seconds=float(avg_holding) if avg_holding is not None else None,
        median_holding_seconds=float(median_holding) if median_holding is not None else None,
        pct_under_5min=(under_5 / count * 100) if count else 0.0,
        pct_under_10min=(under_10 / count * 100) if count else 0.0,
        pct_under_20min=(under_20 / count * 100) if count else 0.0,
        longest_holding_seconds=float(longest_row[0]) if longest_row else None,
        longest_trade_symbol=longest_row[1] if longest_row else None,
        longest_trade_executed_at=longest_row[2] if longest_row else None,
    )
