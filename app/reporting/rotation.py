"""Capital Rotation reporting (Fast-Rotation spec, sections 6-9, 33-34, 54).

Answers the spec's own framing question (section 55): with a given amount
of capital, how many times does it actually get recycled in a day, taking
only opportunities whose net expectation stays positive after costs?
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import HoldingTimeCategory
from app.database.models import OpportunityRecord, SimulatedTradeRecord

EXECUTED_STATUSES = ("simulated_executed", "partial_fill")


@dataclass(slots=True)
class RotationReport:
    period_start: datetime
    period_end: datetime
    portfolio_name: str
    base_capital_usd: float
    total_capital_traded_usd: float
    capital_rotation_rate: float  # total capital traded / base capital — spec section 6
    completed_trades: int
    win_count: int
    loss_count: int
    net_pnl_usd: float
    avg_net_profit_per_trade_usd: float
    avg_holding_time_seconds: float | None
    trades_per_hour: float


async def build_rotation_report(
    session: AsyncSession,
    portfolio_id: int,
    portfolio_name: str,
    base_capital_usd: float,
    mode: str | None = None,
    hours: float = 24.0,
    now: datetime | None = None,
) -> RotationReport:
    """`mode`: None combines Fast + Carry; "fast" = everything under the
    30-minute Maximum Holding Time (spec section 2's default mode — Ultra
    Fast + Fast + Medium combined); "carry" isolates Carry Mode. Kept as a
    plain string rather than HoldingTimeCategory since "fast" here means
    "not carry", not one specific bucket."""
    if mode not in (None, "fast", "carry"):
        raise ValueError(f"mode must be None, 'fast', or 'carry', got {mode!r}")

    now = now or datetime.now(UTC).replace(tzinfo=None)
    period_start = now - timedelta(hours=hours)

    win_case = case((SimulatedTradeRecord.net_profit_usd > 0, 1), else_=0)
    loss_case = case((SimulatedTradeRecord.net_profit_usd < 0, 1), else_=0)
    query = (
        select(
            func.count(),
            func.coalesce(func.sum(SimulatedTradeRecord.capital_usd), 0),
            func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0),
            func.avg(OpportunityRecord.holding_period_seconds),
            func.coalesce(func.sum(win_case), 0),
            func.coalesce(func.sum(loss_case), 0),
        )
        .select_from(SimulatedTradeRecord)
        .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
        .where(
            SimulatedTradeRecord.portfolio_id == portfolio_id,
            SimulatedTradeRecord.executed_at >= period_start,
            SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
        )
    )
    if mode == "carry":
        query = query.where(OpportunityRecord.holding_time_category == HoldingTimeCategory.CARRY)
    elif mode == "fast":
        query = query.where(OpportunityRecord.holding_time_category != HoldingTimeCategory.CARRY)

    count, total_capital, net_pnl, avg_holding, wins, losses = (await session.execute(query)).first()

    total_capital = float(total_capital)
    net_pnl = float(net_pnl)
    return RotationReport(
        period_start=period_start,
        period_end=now,
        portfolio_name=portfolio_name,
        base_capital_usd=base_capital_usd,
        total_capital_traded_usd=total_capital,
        capital_rotation_rate=(total_capital / base_capital_usd) if base_capital_usd else 0.0,
        completed_trades=count,
        win_count=int(wins),
        loss_count=int(losses),
        net_pnl_usd=net_pnl,
        avg_net_profit_per_trade_usd=(net_pnl / count) if count else 0.0,
        avg_holding_time_seconds=float(avg_holding) if avg_holding is not None else None,
        trades_per_hour=(count / hours) if hours else 0.0,
    )
