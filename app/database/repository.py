"""Thin persistence helpers for the tables that already have a clear write path."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Base, Exchange, OpportunityRecord, PriceSnapshot, SimulatedTradeRecord, SystemEvent, VirtualPortfolioRecord
from app.database.session import engine
from app.market_data.normalizer import NormalizedQuote
from app.opportunity.models import Opportunity
from app.opportunity.tracker import TrackedOpportunity
from app.simulation.paper_trader import SimulatedTrade


async def create_all_tables() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_or_create_exchange(session: AsyncSession, name: str, display_name: str) -> Exchange:
    result = await session.execute(select(Exchange).where(Exchange.name == name))
    exchange = result.scalar_one_or_none()
    if exchange is None:
        exchange = Exchange(name=name, display_name=display_name)
        session.add(exchange)
        await session.flush()
    return exchange


async def save_opportunity(session: AsyncSession, opportunity: Opportunity) -> OpportunityRecord:
    edge = opportunity.net_spread_pct if opportunity.net_spread_pct is not None else opportunity.gross_spread_pct
    record = OpportunityRecord(
        id=opportunity.id,
        strategy=opportunity.strategy,
        symbol=opportunity.symbol,
        legs=opportunity.legs,
        gross_spread_pct=opportunity.gross_spread_pct,
        net_spread_pct=opportunity.net_spread_pct,
        break_even_pct=opportunity.break_even_pct,
        capital_usd=opportunity.capital_usd,
        expected_profit_usd=opportunity.expected_profit_usd,
        score=opportunity.score,
        classification=opportunity.classification,
        status=opportunity.status,
        execution_mode=opportunity.execution_mode,
        execution_fill_probability=opportunity.execution_fill_probability,
        market_data_age_seconds=opportunity.market_data_age_seconds,
        annualized_pct=opportunity.annualized_pct,
        days_to_expiry=opportunity.days_to_expiry,
        holding_period_seconds=opportunity.holding_period_seconds,
        holding_time_category=opportunity.holding_time_category,
        capital_is_liquidity_capped=opportunity.capital_is_liquidity_capped,
        capital_velocity_score=opportunity.capital_velocity_score,
        return_per_minute_pct=opportunity.return_per_minute_pct,
        max_spread_pct=edge,
        min_spread_pct=edge,
        avg_spread_pct=edge,
        updates_count=1,
        rejection_reason=opportunity.rejection_reason,
    )
    session.add(record)
    await session.flush()
    return record


async def update_opportunity_tracking(
    session: AsyncSession, tracked: TrackedOpportunity, rejection_reason: str | None = None
) -> None:
    """A continuation of an already-tracked opportunity (Continuous
    Execution spec, sections 5-11) — updates the one existing row's running
    stats instead of inserting a duplicate for the same economic event.
    `rejection_reason` reflects this latest observation's validation
    outcome (sections 12-15), since a signal can drift in or out of being
    worth attempting as the market moves."""
    await session.execute(
        update(OpportunityRecord)
        .where(OpportunityRecord.id == tracked.opportunity_id)
        .values(
            status=tracked.status,
            max_spread_pct=tracked.max_edge_pct,
            min_spread_pct=tracked.min_edge_pct,
            avg_spread_pct=tracked.avg_edge_pct,
            updates_count=tracked.updates_count,
            rejection_reason=rejection_reason,
        )
    )


async def close_opportunity_tracking(session: AsyncSession, tracked: TrackedOpportunity, closed_at: float | None = None) -> None:
    """The signal has gone quiet (OpportunityTracker.expire_stale) without
    ever being traded — mark it closed so it stops looking perpetually ACTIVE."""
    closed_dt = datetime.fromtimestamp(closed_at, tz=UTC).replace(tzinfo=None) if closed_at is not None else datetime.now(UTC).replace(tzinfo=None)
    await session.execute(
        update(OpportunityRecord)
        .where(OpportunityRecord.id == tracked.opportunity_id)
        .values(
            status=tracked.status,
            max_spread_pct=tracked.max_edge_pct,
            min_spread_pct=tracked.min_edge_pct,
            avg_spread_pct=tracked.avg_edge_pct,
            updates_count=tracked.updates_count,
            closed_at=closed_dt,
        )
    )


async def save_price_snapshots(session: AsyncSession, quotes: list[NormalizedQuote]) -> None:
    session.add_all([PriceSnapshot(exchange=q.exchange, symbol=q.symbol, bid=q.bid, ask=q.ask) for q in quotes])
    await session.flush()


async def log_system_event(session: AsyncSession, event_type: str, severity: str, message: str, metadata: dict | None = None) -> SystemEvent:
    event = SystemEvent(event_type=event_type, severity=severity, message=message, event_metadata=metadata)
    session.add(event)
    await session.flush()
    return event


async def get_or_create_portfolio(session: AsyncSession, name: str, initial_capital_usd: float) -> VirtualPortfolioRecord:
    result = await session.execute(select(VirtualPortfolioRecord).where(VirtualPortfolioRecord.name == name))
    portfolio = result.scalar_one_or_none()
    if portfolio is None:
        portfolio = VirtualPortfolioRecord(name=name, initial_capital_usd=initial_capital_usd)
        session.add(portfolio)
        await session.flush()
    return portfolio


async def save_simulated_trade(
    session: AsyncSession, trade: SimulatedTrade, opportunity_id: uuid.UUID, portfolio_id: int
) -> SimulatedTradeRecord:
    record = SimulatedTradeRecord(
        opportunity_id=opportunity_id,
        portfolio_id=portfolio_id,
        status=trade.status,
        capital_usd=trade.capital_usd,
        gross_profit_usd=trade.gross_profit_usd,
        fees_usd=trade.fees_usd,
        slippage_usd=0.0,
        net_profit_usd=trade.net_profit_usd,
    )
    session.add(record)
    await session.flush()
    return record
