"""Thin persistence helpers for the tables that already have a clear write path."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Base, Exchange, OpportunityRecord, PriceSnapshot, SimulatedTradeRecord, SystemEvent, VirtualPortfolioRecord
from app.database.session import engine
from app.market_data.normalizer import NormalizedQuote
from app.opportunity.models import Opportunity
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
    record = OpportunityRecord(
        id=opportunity.id,
        strategy=opportunity.strategy,
        symbol=opportunity.symbol,
        legs=opportunity.legs,
        gross_spread_pct=opportunity.gross_spread_pct,
        net_spread_pct=opportunity.net_spread_pct,
        capital_usd=opportunity.capital_usd,
        expected_profit_usd=opportunity.expected_profit_usd,
        score=opportunity.score,
        classification=opportunity.classification,
        status=opportunity.status,
    )
    session.add(record)
    await session.flush()
    return record


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
        capital_usd=trade.capital_usd,
        gross_profit_usd=trade.gross_profit_usd,
        fees_usd=trade.fees_usd,
        slippage_usd=0.0,
        net_profit_usd=trade.net_profit_usd,
    )
    session.add(record)
    await session.flush()
    return record
