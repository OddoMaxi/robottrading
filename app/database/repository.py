"""Thin persistence helpers for the tables that already have a clear write path."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Base,
    CexScanEventRecord,
    DexSimulatedTradeRecord,
    Exchange,
    OpportunityRecord,
    PriceSnapshot,
    SimulatedTradeRecord,
    SystemEvent,
    VirtualPortfolioRecord,
)
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
        theoretical_edge_pct=opportunity.theoretical_edge_pct,
        depth_adjusted_edge_pct=opportunity.depth_adjusted_edge_pct,
        realistic_executable_edge_pct=opportunity.realistic_executable_edge_pct,
        optimal_capital_usd=opportunity.optimal_capital_usd,
        max_profitable_capital_usd=opportunity.max_profitable_capital_usd,
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
    session: AsyncSession, tracked: TrackedOpportunity, opportunity: Opportunity, rejection_reason: str | None = None
) -> None:
    """A continuation of an already-tracked opportunity (Continuous
    Execution spec, sections 5-11) — updates the one existing row's running
    stats instead of inserting a duplicate for the same economic event.
    `rejection_reason` reflects this latest observation's validation
    outcome (sections 12-15), since a signal can drift in or out of being
    worth attempting as the market moves.

    Execution-engine audit finding (pre-live-trading audit): this used to
    only refresh the min/max/avg tracking fields and rejection_reason,
    leaving net_spread_pct/classification/expected_profit_usd frozen at
    whatever the FIRST observation computed — found live, 803K rows with
    classification='not_profitable' but rejection_reason=NULL (the
    opportunity had since improved and was approved, but its stored
    classification never caught up), making the row's own columns
    self-contradictory. Now refreshes every "current snapshot" field from
    the latest Opportunity object, not just the aggregate stats."""
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
            gross_spread_pct=opportunity.gross_spread_pct,
            net_spread_pct=opportunity.net_spread_pct,
            break_even_pct=opportunity.break_even_pct,
            capital_usd=opportunity.capital_usd,
            expected_profit_usd=opportunity.expected_profit_usd,
            classification=opportunity.classification,
            score=opportunity.score,
            execution_mode=opportunity.execution_mode,
            execution_fill_probability=opportunity.execution_fill_probability,
            market_data_age_seconds=opportunity.market_data_age_seconds,
            theoretical_edge_pct=opportunity.theoretical_edge_pct,
            depth_adjusted_edge_pct=opportunity.depth_adjusted_edge_pct,
            realistic_executable_edge_pct=opportunity.realistic_executable_edge_pct,
            optimal_capital_usd=opportunity.optimal_capital_usd,
            max_profitable_capital_usd=opportunity.max_profitable_capital_usd,
        )
    )


async def close_orphaned_opportunity_tracking(session: AsyncSession, now: datetime | None = None) -> int:
    """Restart-amnesia fix (same bug class as app.simulation.state_recovery
    fixes for capital/positions) — OpportunityTracker is pure in-memory
    state; every process restart starts with an empty tracker, orphaning
    any row still 'detected'/'active' in the DB that the previous process
    was watching, since nothing in the new process's lifecycle will ever
    call close_opportunity_tracking() on a key it never saw. Found live:
    3.76M rows stuck at 'detected' after two days of restarts across a
    single deploy session. Run once at startup, before detection starts —
    bulk-closes every such row as expired rather than leaving it stuck
    forever; a signal that's genuinely still happening gets freshly
    re-detected as a brand new row on the very next scan anyway, which is
    the correct behavior regardless."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    result = await session.execute(
        update(OpportunityRecord).where(OpportunityRecord.status.in_(["detected", "active"])).values(status="expired", closed_at=now)
    )
    return result.rowcount


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


async def save_dex_trade_attempt(session: AsyncSession, attempt) -> DexSimulatedTradeRecord:
    """attempt: app.onchain.dex_paper_trader.DexTradeAttempt. Own table
    (dex_simulated_trades), never simulated_trades — see that model's own
    docstring for why."""
    record = DexSimulatedTradeRecord(
        opportunity_id=attempt.opportunity_id,
        strategy=attempt.strategy,
        symbol=attempt.symbol,
        chain=attempt.chain,
        status=attempt.status.value,
        capital_usd=attempt.capital_usd,
        net_profit_usd=attempt.net_profit_usd,
        revalidated_net_pct=attempt.revalidated_net_pct,
        detection_at=datetime.fromtimestamp(attempt.detection_timestamp, tz=UTC).replace(tzinfo=None),
        validation_at=datetime.fromtimestamp(attempt.validation_timestamp, tz=UTC).replace(tzinfo=None),
        execution_attempt_at=datetime.fromtimestamp(attempt.execution_attempt_timestamp, tz=UTC).replace(tzinfo=None),
        execution_complete_at=datetime.fromtimestamp(attempt.execution_complete_timestamp, tz=UTC).replace(tzinfo=None),
        detection_to_validation_ms=attempt.detection_to_validation_ms,
        validation_to_execution_ms=attempt.validation_to_execution_ms,
        total_execution_ms=attempt.total_execution_ms,
    )
    session.add(record)
    await session.flush()
    return record


async def save_cex_scan_event(
    session: AsyncSession,
    scan_id: str,
    scanned_at: datetime,
    opportunity_id: uuid.UUID,
    is_new_detection: bool,
    strategy: str,
    symbol: str,
    legs: list[dict],
    expected_profit_usd: float | None,
    capital_usd: float | None,
    net_spread_pct: float | None,
    execution_fill_probability: float | None,
    holding_period_seconds: float | None,
    capital_velocity_score: float | None,
    position_already_open: bool,
    old_approved: bool,
    old_rejection_reason: str | None,
) -> CexScanEventRecord:
    """PHASE 2B (user directive, 2026-08-22) — pure telemetry write, called
    from main.py's CEX detection_loop wrapped in its own try/except (see
    that call site) so a failure here can never interrupt OLD's real
    trade processing. Takes plain values, not the live Opportunity/
    ValidationResult/OpenPositionTracker objects themselves — this
    function has no way to mutate anything OLD uses, only to copy
    already-decided values into a new, dedicated row."""
    record = CexScanEventRecord(
        scan_id=scan_id,
        scanned_at=scanned_at,
        opportunity_id=opportunity_id,
        is_new_detection=is_new_detection,
        strategy=strategy,
        symbol=symbol,
        legs=legs,
        expected_profit_usd=expected_profit_usd,
        capital_usd=capital_usd,
        net_spread_pct=net_spread_pct,
        execution_fill_probability=execution_fill_probability,
        holding_period_seconds=holding_period_seconds,
        capital_velocity_score=capital_velocity_score,
        position_already_open=position_already_open,
        old_approved=old_approved,
        old_rejection_reason=old_rejection_reason,
    )
    session.add(record)
    await session.flush()
    return record
