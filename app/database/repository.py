"""Thin persistence helpers for the tables that already have a clear write path."""

import uuid
from datetime import UTC, datetime

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    AltcoinScanObservationRecord,
    Base,
    CexScanEventRecord,
    DexSimulatedTradeRecord,
    DualLegObservationRecord,
    Exchange,
    FullUniverseScanStatusRecord,
    InventoryConstitutionRecord,
    LiveArbitrageExecutionRecord,
    MicroLiveObservationRecord,
    MissedOpportunitySummaryRecord,
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


async def save_micro_live_observation(session: AsyncSession, quote, strategy: str) -> MicroLiveObservationRecord:
    """PHASE 2E (user directive, 2026-08-23) — persists one Binance
    dry-run reality-quote observation. Takes app.execution.reality_quote.RealityQuote
    (not type-hinted directly to avoid a database<->execution import
    cycle) — a plain-value copy, same discipline as save_cex_scan_event.
    Called from main.py's CEX detection loop in the same try/except as
    the reality-quote computation itself; a write failure here can never
    affect paper execution."""
    record = MicroLiveObservationRecord(
        opportunity_id=quote.opportunity_id,
        symbol=quote.symbol,
        strategy=strategy,
        observed_at=datetime.fromtimestamp(quote.computed_at, tz=UTC).replace(tzinfo=None),
        master_requested_size_usd=quote.master_requested_size_usd,
        exchange_valid_size_usd=quote.exchange_valid_size_usd,
        best_bid=quote.best_bid,
        best_ask=quote.best_ask,
        book_spread_pct=quote.book_spread_pct,
        available_depth_usd=quote.available_depth_usd,
        gross_expected_profit_usd=quote.gross_expected_profit_usd,
        maker_fee_rate=quote.maker_fee_rate,
        taker_fee_rate=quote.taker_fee_rate,
        fee_source=quote.fee_source,
        estimated_fees_usd=quote.estimated_fees_usd,
        estimated_slippage_pct=quote.estimated_slippage_pct,
        net_expected_profit_usd=quote.estimated_net_profit_after_real_constraints_usd,
        net_return_bps=quote.net_return_bps,
        min_notional_pass=quote.min_notional_pass,
        lot_size_pass=quote.lot_size_pass,
        balance_pass=quote.balance_pass,
        executable=quote.executable,
        rejection_reason=quote.reason,
    )
    session.add(record)
    await session.flush()
    return record


async def save_dual_leg_observation(session: AsyncSession, quote, strategy: str) -> DualLegObservationRecord:
    """PHASE 2F (user directive, 2026-08-23) — persists one dual-leg
    arbitrage recomputation. Takes app.execution.dual_leg_quote.DualLegQuote
    (not type-hinted directly, same database<->execution import-cycle
    avoidance as save_micro_live_observation). Called from main.py's CEX
    detection loop in the same try/except as the quote computation
    itself; a write failure here can never affect paper execution."""
    record = DualLegObservationRecord(
        opportunity_id=quote.opportunity_id,
        symbol=quote.symbol,
        strategy=strategy,
        observed_at=datetime.fromtimestamp(quote.computed_at, tz=UTC).replace(tzinfo=None),
        buy_exchange=quote.buy_exchange,
        sell_exchange=quote.sell_exchange,
        buy_execution_price=quote.buy_execution_price,
        sell_execution_price=quote.sell_execution_price,
        executable_qty=quote.executable_qty,
        gross_spread_pct=quote.gross_spread_pct,
        buy_fee_usd=quote.buy_fee_usd,
        sell_fee_usd=quote.sell_fee_usd,
        buy_slippage_pct=quote.buy_slippage_pct,
        sell_slippage_pct=quote.sell_slippage_pct,
        buy_fee_source=quote.buy_fee_source,
        sell_fee_source=quote.sell_fee_source,
        buy_quote_age_ms=quote.buy_quote_age_ms,
        sell_quote_age_ms=quote.sell_quote_age_ms,
        dual_leg_latency_ms=quote.dual_leg_latency_ms,
        net_profit_usd=quote.net_profit_usd,
        net_return_bps=quote.net_return_bps,
        buy_min_notional_pass=quote.buy_min_notional_pass,
        buy_lot_size_pass=quote.buy_lot_size_pass,
        sell_min_notional_pass=quote.sell_min_notional_pass,
        sell_lot_size_pass=quote.sell_lot_size_pass,
        buy_tradable=quote.buy_tradable,
        sell_tradable=quote.sell_tradable,
        executable=quote.executable,
        rejection_reason=quote.reason,
    )
    session.add(record)
    await session.flush()
    return record


async def save_live_arbitrage_execution(session: AsyncSession, result) -> LiveArbitrageExecutionRecord:
    """PROFIT REALITY LEDGER (Phase 3A, user directive, 2026-08-23).
    Persists one app.execution.live_arbitrage_executor.LiveArbitrageResult
    (not type-hinted directly, same database<->execution import-cycle
    avoidance as the other save_* functions here) — for EVERY outcome,
    not just BOTH_FILLED, so the ledger has no silent gap for an attempt
    that was actually made. This table stays empty until an operator
    explicitly enables live trading and calls execute_one_arbitrage()."""

    def _latency_ms(submitted_at: float | None, confirmed_at: float | None) -> float | None:
        if submitted_at is None or confirmed_at is None:
            return None
        return (confirmed_at - submitted_at) * 1000

    def _realized_spread_pct(buy_price: float | None, sell_price: float | None) -> float | None:
        if not buy_price or not sell_price:
            return None
        return (sell_price - buy_price) / buy_price * 100

    record = LiveArbitrageExecutionRecord(
        attempt_id=result.attempt_id,
        symbol=result.symbol,
        buy_exchange=result.buy_exchange,
        sell_exchange=result.sell_exchange,
        outcome=result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome),
        reason=result.reason,
        started_at=datetime.fromtimestamp(result.started_at, tz=UTC).replace(tzinfo=None),
        completed_at=(
            datetime.fromtimestamp(result.completed_at, tz=UTC).replace(tzinfo=None) if result.completed_at is not None else None
        ),
        predicted_net_profit_usd=result.predicted_net_profit_usd,
        predicted_fees_usd=result.predicted_fees_usd,
        predicted_slippage_pct=result.predicted_slippage_pct,
        safety_adjusted_predicted_profit_usd=result.safety_adjusted_predicted_profit_usd,
        buy_client_order_id=result.buy_client_order_id,
        buy_exchange_order_id=result.buy_exchange_order_id,
        buy_status=result.buy_status,
        buy_filled_qty=result.buy_filled_qty,
        buy_net_filled_qty=result.buy_net_filled_qty,
        buy_avg_fill_price=result.buy_avg_fill_price,
        buy_fee_asset=result.buy_fee_asset,
        buy_fee_amount=result.buy_fee_amount,
        buy_fees_usd=result.buy_fees_usd,
        buy_latency_ms=_latency_ms(result.buy_submitted_at, result.buy_confirmed_at),
        sell_client_order_id=result.sell_client_order_id,
        sell_exchange_order_id=result.sell_exchange_order_id,
        sell_status=result.sell_status,
        sell_filled_qty=result.sell_filled_qty,
        sell_avg_fill_price=result.sell_avg_fill_price,
        sell_fee_asset=result.sell_fee_asset,
        sell_fee_amount=result.sell_fee_amount,
        sell_fees_usd=result.sell_fees_usd,
        sell_latency_ms=_latency_ms(result.sell_submitted_at, result.sell_confirmed_at),
        neutralization_order_id=result.neutralization_order_id,
        neutralization_filled_qty=result.neutralization_filled_qty,
        actual_realized_spread_pct=_realized_spread_pct(result.buy_avg_fill_price, result.sell_avg_fill_price),
        actual_net_pnl_usd=result.actual_net_pnl_usd,
        prediction_error_usd=result.prediction_error_usd,
    )
    session.add(record)
    await session.flush()
    return record


async def save_inventory_constitution_result(session: AsyncSession, result) -> InventoryConstitutionRecord:
    """AUTOMATIC INVENTORY CONSTITUTION LEDGER (user directive,
    2026-08-24). Persists one app.execution.inventory_constitution_executor.
    InventoryConstitutionResult (not type-hinted directly, same
    database<->execution import-cycle avoidance as the other save_*
    functions here) — for EVERY outcome, not just FILLED, so the ledger
    has no silent gap for an attempt that was actually made."""

    def _latency_ms(submitted_at: float | None, confirmed_at: float | None) -> float | None:
        if submitted_at is None or confirmed_at is None:
            return None
        return (confirmed_at - submitted_at) * 1000

    record = InventoryConstitutionRecord(
        attempt_id=result.attempt_id,
        symbol=result.symbol,
        buy_exchange_for_arbitrage=result.buy_exchange_for_arbitrage,
        sell_exchange=result.sell_exchange,
        outcome=result.outcome.value if hasattr(result.outcome, "value") else str(result.outcome),
        reason=result.reason,
        started_at=datetime.fromtimestamp(result.started_at, tz=UTC).replace(tzinfo=None),
        completed_at=(
            datetime.fromtimestamp(result.completed_at, tz=UTC).replace(tzinfo=None) if result.completed_at is not None else None
        ),
        pre_purchase_net_edge_usd=result.pre_purchase_net_edge_usd,
        required_base_qty=result.required_base_qty,
        requested_notional_usdt=result.requested_notional_usdt,
        order_client_id=result.order_client_id,
        order_exchange_id=result.order_exchange_id,
        order_status=result.order_status,
        filled_qty=result.filled_qty,
        net_filled_qty=result.net_filled_qty,
        avg_fill_price=result.avg_fill_price,
        fee_asset=result.fee_asset,
        fee_amount=result.fee_amount,
        fee_usd=result.fee_usd,
        latency_ms=_latency_ms(result.submitted_at, result.confirmed_at),
        post_fill_net_edge_usd=result.post_fill_net_edge_usd,
        edge_still_valid_after_fill=result.edge_still_valid_after_fill,
        max_safe_arbitrage_notional_usdt=result.max_safe_arbitrage_notional_usdt,
        ready_for_arbitrage=result.ready_for_arbitrage,
    )
    session.add(record)
    await session.flush()
    return record


LIVE_MARKET_EXCHANGES = ("binance", "bybit")  # the only exchanges with a real trade client — see item 2, 2026-08-24


async def save_altcoin_scan_observation(
    session: AsyncSession,
    direction_quote,
    observed_at: datetime,
    continuity_status: str,
    persistence_seconds: float,
) -> AltcoinScanObservationRecord:
    """ALTCOIN SCANNER (user directive, 2026-08-23). Persists one
    (symbol, buy_exchange, sell_exchange) observation from
    app.scanner.cross_exchange_scanner.DirectionQuote. Written by the
    standalone altcoin_scanner.py process — never by main.py.

    market_scope is derived, never passed in, so it can't drift from the
    row's own buy_exchange/sell_exchange (item 2, 2026-08-24: "aucune
    opportunité impliquant OKX ne doit apparaître comme LIVE")."""
    quote = direction_quote.quote
    reference_notional_usd = quote.executable_qty * quote.buy_execution_price
    net_profit_per_1000usdt = (
        quote.net_profit_usd / reference_notional_usd * 1000 if reference_notional_usd > 0 else 0.0
    )
    market_scope = "live" if direction_quote.buy_exchange in LIVE_MARKET_EXCHANGES and direction_quote.sell_exchange in LIVE_MARKET_EXCHANGES else "research"
    record = AltcoinScanObservationRecord(
        symbol=direction_quote.symbol,
        buy_exchange=direction_quote.buy_exchange,
        sell_exchange=direction_quote.sell_exchange,
        observed_at=observed_at,
        buy_execution_price=quote.buy_execution_price,
        sell_execution_price=quote.sell_execution_price,
        gross_spread_pct=quote.gross_spread_pct,
        buy_fee_rate=quote.buy_fee_usd / reference_notional_usd if reference_notional_usd > 0 else 0.0,
        sell_fee_rate=quote.sell_fee_usd / reference_notional_usd if reference_notional_usd > 0 else 0.0,
        buy_fee_source=quote.buy_fee_source,
        sell_fee_source=quote.sell_fee_source,
        buy_slippage_pct=quote.buy_slippage_pct,
        sell_slippage_pct=quote.sell_slippage_pct,
        available_depth_usd=reference_notional_usd,
        executable_qty=quote.executable_qty,
        net_profit_usd=quote.net_profit_usd,
        net_return_bps=quote.net_return_bps,
        net_profit_per_1000usdt=net_profit_per_1000usdt,
        executable=quote.executable,
        rejection_reason=quote.reason,
        continuity_status=continuity_status,
        persistence_seconds=persistence_seconds,
        market_scope=market_scope,
    )
    session.add(record)
    await session.flush()
    return record


async def upsert_full_universe_scan_status(
    session: AsyncSession,
    updated_at: datetime,
    common_pairs_count: int,
    pairs_fast_scanned: int,
    pairs_raw_spread_stage_a: int,
    pairs_deep_validated: int,
    pairs_net_positive_stage_b_live: int,
    cycle_duration_seconds: float,
) -> FullUniverseScanStatusRecord:
    """INVENTORY MANAGER V2 (user directive, 2026-08-24). Always id=1 —
    overwrites the single row rather than inserting a new one, so this
    table never grows. Written by the standalone altcoin_scanner.py
    process; read by GET /live/full-universe-discovery in the engine
    process, which has no other way to see the scanner's live counters."""
    existing = await session.get(FullUniverseScanStatusRecord, 1)
    if existing is None:
        session.add(
            FullUniverseScanStatusRecord(
                id=1, updated_at=updated_at, common_pairs_count=common_pairs_count,
                pairs_fast_scanned=pairs_fast_scanned, pairs_raw_spread_stage_a=pairs_raw_spread_stage_a,
                pairs_deep_validated=pairs_deep_validated, pairs_net_positive_stage_b_live=pairs_net_positive_stage_b_live,
                cycle_duration_seconds=cycle_duration_seconds,
            )
        )
    else:
        existing.updated_at = updated_at
        existing.common_pairs_count = common_pairs_count
        existing.pairs_fast_scanned = pairs_fast_scanned
        existing.pairs_raw_spread_stage_a = pairs_raw_spread_stage_a
        existing.pairs_deep_validated = pairs_deep_validated
        existing.pairs_net_positive_stage_b_live = pairs_net_positive_stage_b_live
        existing.cycle_duration_seconds = cycle_duration_seconds
    await session.flush()
    result = await session.get(FullUniverseScanStatusRecord, 1)
    assert result is not None
    return result


async def get_full_universe_scan_status(session: AsyncSession) -> FullUniverseScanStatusRecord | None:
    return await session.get(FullUniverseScanStatusRecord, 1)


async def upsert_missed_opportunity_summaries(
    session: AsyncSession,
    summaries: dict[str, tuple[int, float]],  # cause -> (count, theoretical_profit_usd_total)
    updated_at: datetime,
) -> None:
    """MISSED PROFITABLE OPPORTUNITIES (V2.1, user directive, 2026-08-24,
    item 5). One upsert per cause — cause is the primary key, so this
    never grows past len(ALL_CAUSES) rows. Written by altcoin_scanner.py
    each cycle from its cumulative in-process MissedOpportunityTracker;
    also called by the engine process for the three balance/concurrency
    causes it alone can observe (see app.reporting.missed_opportunity_report)."""
    for cause, (count, theoretical_profit_usd_total) in summaries.items():
        existing = await session.get(MissedOpportunitySummaryRecord, cause)
        if existing is None:
            session.add(
                MissedOpportunitySummaryRecord(
                    cause=cause, count=count, theoretical_profit_usd_total=theoretical_profit_usd_total, updated_at=updated_at,
                )
            )
        else:
            existing.count = count
            existing.theoretical_profit_usd_total = theoretical_profit_usd_total
            existing.updated_at = updated_at
    await session.flush()


async def get_missed_opportunity_summaries(session: AsyncSession) -> list[MissedOpportunitySummaryRecord]:
    result = await session.execute(select(MissedOpportunitySummaryRecord))
    return list(result.scalars().all())
