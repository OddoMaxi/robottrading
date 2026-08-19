"""Daily Summary (Net Opportunity Engine spec, section 19)."""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord

SUCCESSFUL_STATUSES = ("simulated_executed", "partial_fill")


@dataclass(slots=True)
class DailySummary:
    period_start: datetime
    period_end: datetime
    detected: int
    net_positive: int
    paper_trades: int
    successful_simulations: int
    simulated_net_pnl_usd: float
    best_strategy: str | None
    worst_strategy: str | None
    best_asset: str | None


async def build_daily_summary(session: AsyncSession, now: datetime | None = None) -> DailySummary:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    period_start = now - timedelta(hours=24)

    detected = (
        await session.execute(select(func.count()).where(OpportunityRecord.detected_at >= period_start))
    ).scalar() or 0

    net_positive = (
        await session.execute(
            select(func.count()).where(OpportunityRecord.detected_at >= period_start, OpportunityRecord.net_spread_pct > 0)
        )
    ).scalar() or 0

    paper_trades = (
        await session.execute(select(func.count()).where(SimulatedTradeRecord.executed_at >= period_start))
    ).scalar() or 0

    successful_simulations = (
        await session.execute(
            select(func.count()).where(
                SimulatedTradeRecord.executed_at >= period_start, SimulatedTradeRecord.status.in_(SUCCESSFUL_STATUSES)
            )
        )
    ).scalar() or 0

    simulated_net_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0)).where(
                SimulatedTradeRecord.executed_at >= period_start
            )
        )
    ).scalar() or 0.0

    strategy_rows = (
        await session.execute(
            select(OpportunityRecord.strategy, func.avg(OpportunityRecord.net_spread_pct))
            .where(OpportunityRecord.detected_at >= period_start, OpportunityRecord.net_spread_pct.isnot(None))
            .group_by(OpportunityRecord.strategy)
        )
    ).all()
    best_strategy = max(strategy_rows, key=lambda r: r[1])[0] if strategy_rows else None
    worst_strategy = min(strategy_rows, key=lambda r: r[1])[0] if strategy_rows else None

    best_asset_row = (
        await session.execute(
            select(OpportunityRecord.symbol, func.avg(OpportunityRecord.net_spread_pct))
            .where(OpportunityRecord.detected_at >= period_start, OpportunityRecord.net_spread_pct.isnot(None))
            .group_by(OpportunityRecord.symbol)
            .order_by(func.avg(OpportunityRecord.net_spread_pct).desc())
            .limit(1)
        )
    ).first()
    best_asset = best_asset_row[0] if best_asset_row else None

    return DailySummary(
        period_start=period_start,
        period_end=now,
        detected=detected,
        net_positive=net_positive,
        paper_trades=paper_trades,
        successful_simulations=successful_simulations,
        simulated_net_pnl_usd=float(simulated_net_pnl),
        best_strategy=best_strategy,
        worst_strategy=worst_strategy,
        best_asset=best_asset,
    )
