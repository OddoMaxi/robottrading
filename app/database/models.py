"""SQLAlchemy models for the tables listed in section 24."""

import uuid
from datetime import datetime

from sqlalchemy import JSON, ForeignKey, Index, Numeric, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class Exchange(Base):
    __tablename__ = "exchanges"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True, index=True)  # "binance", "okx", "bybit"
    display_name: Mapped[str]
    is_active: Mapped[bool] = mapped_column(default=True)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())

    markets: Mapped[list["Market"]] = relationship(back_populates="exchange")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True)
    symbol: Mapped[str] = mapped_column(unique=True, index=True)  # "BTC", "USDT", ...
    name: Mapped[str | None]


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("exchange_id", "symbol", "market_type"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    symbol: Mapped[str] = mapped_column(index=True)  # common form "BTC/USDT"
    base_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    quote_asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    market_type: Mapped[str]  # spot | perpetual | futures
    is_active: Mapped[bool] = mapped_column(default=True)

    exchange: Mapped[Exchange] = relationship(back_populates="markets")


class OrderBookSnapshot(Base):
    """Depth snapshot (multiple levels) — feeds the Liquidity/Slippage engines."""

    __tablename__ = "orderbooks"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    bids: Mapped[list] = mapped_column(JSON)  # [[price, quantity], ...]
    asks: Mapped[list] = mapped_column(JSON)
    exchange_timestamp: Mapped[datetime]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class Quote(Base):
    """Best bid/ask tick — the normalized model from section 9."""

    __tablename__ = "quotes"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    bid: Mapped[float] = mapped_column(Numeric(20, 10))
    ask: Mapped[float] = mapped_column(Numeric(20, 10))
    bid_quantity: Mapped[float] = mapped_column(Numeric(20, 10))
    ask_quantity: Mapped[float] = mapped_column(Numeric(20, 10))
    exchange_timestamp: Mapped[datetime]
    received_at: Mapped[datetime] = mapped_column(server_default=func.now())


class PriceSnapshot(Base):
    """Lightweight, denormalized bid/ask log for charting price history.

    Deliberately not FK'd through markets/assets (unlike Quote above) — those
    aren't seeded yet, and this table only needs to answer "what did the
    price look like over time" for the dashboard's candlestick charts.
    """

    __tablename__ = "price_snapshots"
    __table_args__ = (
        # Every symbol-scoped query filters by symbol (+ optionally
        # recorded_at) — never by exchange alone — so this one leads with symbol.
        Index("ix_price_snapshots_symbol_recorded_at", "symbol", "recorded_at"),
        # Simple Mode's "is this exchange connected?" check is the one query
        # that filters by exchange alone (latest tick per exchange) — without
        # this, that query would fall back to the symbol-led index (useless
        # here) and scan the whole table, the exact hang this app already got
        # burned by once at ~1M rows.
        Index("ix_price_snapshots_exchange_recorded_at", "exchange", "recorded_at"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange: Mapped[str]
    symbol: Mapped[str]
    bid: Mapped[float] = mapped_column(Numeric(20, 10))
    ask: Mapped[float] = mapped_column(Numeric(20, 10))
    recorded_at: Mapped[datetime] = mapped_column(server_default=func.now())


class FundingRate(Base):
    __tablename__ = "funding_rates"

    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    funding_rate: Mapped[float] = mapped_column(Numeric(12, 8))
    next_funding_time: Mapped[datetime]
    mark_price: Mapped[float] = mapped_column(Numeric(20, 10))
    index_price: Mapped[float] = mapped_column(Numeric(20, 10))
    timestamp: Mapped[datetime] = mapped_column(server_default=func.now())


class OpportunityRecord(Base):
    __tablename__ = "opportunities"
    __table_args__ = (
        # This table grows fast (event-driven detection ⇒ hundreds of
        # thousands of rows within a day) — every dashboard/report query
        # filters or sorts on these, and a missing index here turns into a
        # full table scan that gets slower every day. Learned the hard way:
        # the dashboard started hanging once this table passed ~1M rows.
        Index("ix_opportunities_detected_at", "detected_at", postgresql_using="btree"),
        Index("ix_opportunities_net_spread_pct", "net_spread_pct", postgresql_where=text("net_spread_pct > 0")),
        Index("ix_opportunities_holding_time_category", "holding_time_category"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy: Mapped[str] = mapped_column(index=True)  # stablecoin | cross_exchange | triangular | funding
    symbol: Mapped[str] = mapped_column(index=True)
    legs: Mapped[list] = mapped_column(JSON)

    gross_spread_pct: Mapped[float] = mapped_column(Numeric(10, 6))
    net_spread_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    break_even_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    capital_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    expected_profit_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))

    score: Mapped[float | None] = mapped_column(Numeric(5, 2))
    classification: Mapped[str | None]
    status: Mapped[str] = mapped_column(default="detected")
    # Continuous Execution spec, sections 12-15, 43 — None once APPROVED
    # (or not yet validated); one of app.execution.validator.RejectionReason otherwise.
    rejection_reason: Mapped[str | None]

    execution_mode: Mapped[str | None]
    execution_fill_probability: Mapped[float | None] = mapped_column(Numeric(5, 4))
    market_data_age_seconds: Mapped[float | None] = mapped_column(Numeric(6, 3))
    annualized_pct: Mapped[float | None] = mapped_column(Numeric(12, 4))
    days_to_expiry: Mapped[float | None] = mapped_column(Numeric(8, 2))

    # Fast-Rotation spec — Fast Mode (default) vs Carry Mode split.
    holding_period_seconds: Mapped[float | None] = mapped_column(Numeric(12, 2))
    holding_time_category: Mapped[str | None]
    capital_is_liquidity_capped: Mapped[bool] = mapped_column(default=True)
    capital_velocity_score: Mapped[float | None] = mapped_column(Numeric(5, 1))
    return_per_minute_pct: Mapped[float | None] = mapped_column(Numeric(14, 6))

    # Depth-Adjusted Execution Curve (Opportunity Expansion spec, Step 2,
    # user directive, 2026-08-21) — see app.analytics.execution_depth and
    # app.opportunity.models.Opportunity's matching fields for what each
    # one means. capital_usd/net_spread_pct/expected_profit_usd above are
    # now themselves priced at optimal_capital_usd for a liquidity-capped
    # opportunity — these are additive diagnostic context, not a second
    # copy of the same numbers under new names.
    theoretical_edge_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    depth_adjusted_edge_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    realistic_executable_edge_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    optimal_capital_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    max_profitable_capital_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))

    detected_at: Mapped[datetime] = mapped_column(server_default=func.now())
    peak_at: Mapped[datetime | None]
    closed_at: Mapped[datetime | None]
    max_spread_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    min_spread_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    avg_spread_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    # Continuous Execution spec, sections 8 & 41-42 — how many raw scan
    # ticks observed this same underlying signal (deduplicated via
    # OpportunityTracker) before it either closed or expired. 1 unless a
    # continuation updated it. "observed" in the funnel KPI = sum of this
    # across every row; "unique opportunities" = row count.
    updates_count: Mapped[int] = mapped_column(default=1)


class VirtualPortfolioRecord(Base):
    __tablename__ = "virtual_portfolios"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(unique=True)  # "500", "1K", "5K", "10K", "25K"
    initial_capital_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class SimulatedTradeRecord(Base):
    __tablename__ = "simulated_trades"

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"), index=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("virtual_portfolios.id"), index=True)
    status: Mapped[str] = mapped_column(default="simulated_executed")
    capital_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    gross_profit_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    fees_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    slippage_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    net_profit_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    executed_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DexSimulatedTradeRecord(Base):
    """DEX-specific execution attempts (Multi-Market Opportunity Engine,
    V5.5 continuation, user directive, 2026-08-22) — deliberately its own
    table, not simulated_trades: that table is FK'd to virtual_portfolios
    (CEX's real capital ledger), and DEX must never touch it (spec section
    39). Own capital pool (app.onchain.dex_paper_trader.DexCapitalPool),
    own ledger here, same isolation guarantee proven for detection now
    extended to execution."""

    __tablename__ = "dex_simulated_trades"
    __table_args__ = (Index("ix_dex_simulated_trades_execution_complete_at", "execution_complete_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("opportunities.id"), index=True)
    strategy: Mapped[str] = mapped_column(index=True)
    symbol: Mapped[str]
    chain: Mapped[str]
    status: Mapped[str]
    capital_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    net_profit_usd: Mapped[float] = mapped_column(Numeric(20, 4))
    revalidated_net_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    detection_at: Mapped[datetime]
    validation_at: Mapped[datetime]
    execution_attempt_at: Mapped[datetime]
    execution_complete_at: Mapped[datetime]
    detection_to_validation_ms: Mapped[float] = mapped_column(Numeric(12, 3))
    validation_to_execution_ms: Mapped[float] = mapped_column(Numeric(12, 3))
    total_execution_ms: Mapped[float] = mapped_column(Numeric(12, 3))


class ShadowDecisionRecord(Base):
    """Phase 2 — Global Orchestration, SHADOW MODE ONLY (user directive,
    2026-08-22). One row per already-detected opportunity (CEX or DEX),
    comparing what the real engine actually did (old_engine_*, read from
    the real, already-persisted simulated_trades/dex_simulated_trades —
    never written to) against what app.shadow's own Master Ranker +
    Global Capital Allocator SIMULATION would have decided, against its
    own entirely separate ShadowCapitalLedger (app.shadow.ledger).

    Deliberately NOT foreign-keyed to virtual_portfolios or anything the
    real executors write to — this table is written to by
    shadow_orchestrator.py ONLY, a process that never imports
    app.simulation.paper_trader or app.onchain.dex_paper_trader (see
    app/shadow/__init__.py and tests/test_shadow_isolation.py). Reading
    or deleting rows here can never affect real capital, positions, or
    the real engines' own decisions — this table is pure observation."""

    __tablename__ = "shadow_decisions"
    __table_args__ = (Index("ix_shadow_decisions_decided_at", "decided_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True, unique=True)
    engine: Mapped[str]  # "CEX" or "DEX"
    strategy: Mapped[str] = mapped_column(index=True)
    symbol: Mapped[str]
    detected_at: Mapped[datetime]
    decided_at: Mapped[datetime] = mapped_column(server_default=func.now())

    old_engine_outcome: Mapped[str]
    old_engine_reason: Mapped[str | None]
    old_engine_net_profit_usd: Mapped[float | None] = mapped_column(Numeric(20, 4))
    old_engine_capital_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))

    master_outcome: Mapped[str]
    master_reason: Mapped[str | None]
    master_rank_score: Mapped[float]
    master_capital_reserved_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    master_projected_net_profit_usd: Mapped[float | None] = mapped_column(Numeric(20, 4))
    master_capital_available_global_usd: Mapped[float] = mapped_column(Numeric(20, 2))

    agree: Mapped[bool]


class CexScanEventRecord(Base):
    """PHASE 2B — CEX Scan-Level Shadow telemetry (user directive,
    2026-08-22). Read-only observability tap on main.py's real CEX
    detection_loop: one row per (opportunity, scan cycle) — including
    every continuation, not just "new" detections — capturing exactly
    what OLD decided at that instant. This closes the observability gap
    the Phase 2 final validation found: MASTER previously only ever saw
    "new" opportunities rows, while OLD's real loop re-validates (and can
    re-approve) a persisting opportunity every single scan cycle,
    continuously refreshing app.simulation.position_tracker's lock —
    invisible to any evaluation based on the opportunities table alone.

    Written by main.py's detection_loop via
    app.database.repository.save_cex_scan_event, wrapped in its own
    try/except (see main.py's own instrumentation site) so a telemetry
    failure can NEVER interrupt OLD's real trade processing for this or
    any other opportunity in the same cycle — purely additive, changes
    nothing about what OLD decides or does. Never read by main.py itself
    after being written; only shadow_orchestrator.py reads this table."""

    __tablename__ = "cex_scan_events"
    __table_args__ = (Index("ix_cex_scan_events_scanned_at", "scanned_at"), Index("ix_cex_scan_events_opportunity_id", "opportunity_id"))

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_id: Mapped[str] = mapped_column(index=True)
    scanned_at: Mapped[datetime]
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    is_new_detection: Mapped[bool]
    strategy: Mapped[str]
    symbol: Mapped[str]
    legs: Mapped[list] = mapped_column(JSON)
    expected_profit_usd: Mapped[float | None] = mapped_column(Numeric(20, 4))
    capital_usd: Mapped[float | None] = mapped_column(Numeric(20, 2))
    net_spread_pct: Mapped[float | None] = mapped_column(Numeric(10, 6))
    execution_fill_probability: Mapped[float | None] = mapped_column(Numeric(6, 4))
    holding_period_seconds: Mapped[float | None]
    capital_velocity_score: Mapped[float | None]
    position_already_open: Mapped[bool]  # independently checked at telemetry time, regardless of whether validate() short-circuited on an earlier gate
    old_approved: Mapped[bool]
    old_rejection_reason: Mapped[str | None]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class CexScanShadowDecisionRecord(Base):
    """PHASE 2B (user directive, 2026-08-22) — MASTER's shadow decision
    replayed at the SAME scan-cycle granularity as OLD's real decision
    captured in CexScanEventRecord, evaluated against a DEDICATED,
    separate ShadowCapitalLedger/ShadowOpenPositionTracker instance (not
    shared with the opportunity-level shadow_decisions comparison, which
    remains the DEX-focused, already-validated 98.4-100% agreement
    track) — keeping this genuinely 1:1-comparable analysis internally
    consistent rather than conflating two different granularities of
    simulated capital state."""

    __tablename__ = "cex_scan_shadow_decisions"
    __table_args__ = (Index("ix_cex_scan_shadow_decisions_decided_at", "decided_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scan_event_id: Mapped[int] = mapped_column(ForeignKey("cex_scan_events.id"), unique=True, index=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    scanned_at: Mapped[datetime]
    is_new_detection: Mapped[bool]
    decided_at: Mapped[datetime] = mapped_column(server_default=func.now())

    old_approved: Mapped[bool]
    old_rejection_reason: Mapped[str | None]

    master_outcome: Mapped[str]
    master_reason: Mapped[str | None]

    agree: Mapped[bool]


class Balance(Base):
    __tablename__ = "balances"
    __table_args__ = (UniqueConstraint("portfolio_id", "exchange_id", "asset_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("virtual_portfolios.id"), index=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    amount: Mapped[float] = mapped_column(Numeric(30, 10))
    updated_at: Mapped[datetime] = mapped_column(server_default=func.now(), onupdate=func.now())


class RebalancingEvent(Base):
    __tablename__ = "rebalancing_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    portfolio_id: Mapped[int] = mapped_column(ForeignKey("virtual_portfolios.id"), index=True)
    from_exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    to_exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"))
    asset_id: Mapped[int] = mapped_column(ForeignKey("assets.id"))
    amount: Mapped[float] = mapped_column(Numeric(30, 10))
    network_fee_usd: Mapped[float] = mapped_column(Numeric(20, 2))
    duration_seconds: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LatencyMetricRecord(Base):
    __tablename__ = "latency_metrics"

    id: Mapped[int] = mapped_column(primary_key=True)
    exchange_id: Mapped[int] = mapped_column(ForeignKey("exchanges.id"), index=True)
    exchange_timestamp: Mapped[datetime]
    received_at: Mapped[datetime]
    processing_at: Mapped[datetime]
    detected_at: Mapped[datetime]
    total_latency_ms: Mapped[float]
    component: Mapped[str | None]


class SystemEvent(Base):
    __tablename__ = "system_events"

    id: Mapped[int] = mapped_column(primary_key=True)
    event_type: Mapped[str]
    severity: Mapped[str]  # info | warning | error | critical
    message: Mapped[str]
    event_metadata: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class MicroLiveObservationRecord(Base):
    """PHASE 2E — REAL EDGE VALIDATION (user directive, 2026-08-23).

    One row per Binance dry-run reality-quote observation (Phase 2D built
    the in-memory-only version; this phase persists every observation so
    a much larger, statistically meaningful sample can be analyzed by
    symbol/strategy/time-slice without depending on the engine process's
    memory surviving a restart). Written by
    app.database.repository.save_micro_live_observation, called from
    main.py's CEX detection loop in the same try/except as the reality
    quote computation itself — a write failure here can never affect
    paper execution. Read-only observability: nothing in this table is
    ever read back by the detection loop, only by
    app.reporting.micro_live_edge and the dashboard."""

    __tablename__ = "micro_live_observations"
    __table_args__ = (
        Index("ix_micro_live_observations_observed_at", "observed_at"),
        Index("ix_micro_live_observations_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    symbol: Mapped[str]
    strategy: Mapped[str]
    observed_at: Mapped[datetime]

    master_requested_size_usd: Mapped[float] = mapped_column(Numeric(20, 4))
    exchange_valid_size_usd: Mapped[float] = mapped_column(Numeric(20, 4))
    best_bid: Mapped[float] = mapped_column(Numeric(24, 10))
    best_ask: Mapped[float] = mapped_column(Numeric(24, 10))
    book_spread_pct: Mapped[float] = mapped_column(Numeric(12, 6))
    available_depth_usd: Mapped[float] = mapped_column(Numeric(20, 4))

    gross_expected_profit_usd: Mapped[float] = mapped_column(Numeric(20, 6))
    maker_fee_rate: Mapped[float | None] = mapped_column(Numeric(10, 6))
    taker_fee_rate: Mapped[float] = mapped_column(Numeric(10, 6))
    fee_source: Mapped[str]  # "real_binance_fee" | "estimated_default"
    estimated_fees_usd: Mapped[float] = mapped_column(Numeric(20, 6))
    estimated_slippage_pct: Mapped[float] = mapped_column(Numeric(12, 6))
    net_expected_profit_usd: Mapped[float] = mapped_column(Numeric(20, 6))
    net_return_bps: Mapped[float] = mapped_column(Numeric(14, 4))

    min_notional_pass: Mapped[bool]
    lot_size_pass: Mapped[bool]
    balance_pass: Mapped[bool]
    executable: Mapped[bool]
    rejection_reason: Mapped[str | None]

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class DualLegObservationRecord(Base):
    """PHASE 2F — DUAL-LEG REALITY VALIDATION (user directive, 2026-08-23).

    Phase 2D/2E only ever reality-tested the Binance leg of a
    cross_exchange opportunity. One row per opportunity here captures the
    FULL arbitrage recomputed independently from live, real,
    separately-fetched data on BOTH legs (app.execution.dual_leg_quote) —
    never opp.expected_profit_usd. Written by
    app.database.repository.save_dual_leg_observation, called from
    main.py's CEX detection loop in the same try/except discipline as
    every other Phase 2D/2E/2F telemetry write: a failure here can never
    affect paper execution. Read-only observability — nothing in this
    table is ever read back by the detection loop, only by
    app.reporting.dual_leg_edge and this phase's final report."""

    __tablename__ = "dual_leg_observations"
    __table_args__ = (
        Index("ix_dual_leg_observations_observed_at", "observed_at"),
        Index("ix_dual_leg_observations_symbol", "symbol"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), index=True)
    symbol: Mapped[str]
    strategy: Mapped[str]
    observed_at: Mapped[datetime]

    buy_exchange: Mapped[str]
    sell_exchange: Mapped[str]
    buy_execution_price: Mapped[float] = mapped_column(Numeric(30, 12))
    sell_execution_price: Mapped[float] = mapped_column(Numeric(30, 12))
    executable_qty: Mapped[float] = mapped_column(Numeric(30, 6))

    gross_spread_pct: Mapped[float] = mapped_column(Numeric(12, 6))
    buy_fee_usd: Mapped[float] = mapped_column(Numeric(20, 6))
    sell_fee_usd: Mapped[float] = mapped_column(Numeric(20, 6))
    buy_slippage_pct: Mapped[float] = mapped_column(Numeric(12, 6))
    sell_slippage_pct: Mapped[float] = mapped_column(Numeric(12, 6))
    buy_fee_source: Mapped[str]  # "real_account_fee" | "estimated_default"
    sell_fee_source: Mapped[str]

    buy_quote_age_ms: Mapped[float] = mapped_column(Numeric(14, 3))
    sell_quote_age_ms: Mapped[float] = mapped_column(Numeric(14, 3))
    dual_leg_latency_ms: Mapped[float] = mapped_column(Numeric(14, 3))

    net_profit_usd: Mapped[float] = mapped_column(Numeric(20, 6))
    net_return_bps: Mapped[float] = mapped_column(Numeric(14, 4))

    buy_min_notional_pass: Mapped[bool]
    buy_lot_size_pass: Mapped[bool]
    sell_min_notional_pass: Mapped[bool]
    sell_lot_size_pass: Mapped[bool]
    buy_tradable: Mapped[bool]
    sell_tradable: Mapped[bool]
    executable: Mapped[bool]
    rejection_reason: Mapped[str | None]

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())


class LiveArbitrageExecutionRecord(Base):
    """PROFIT REALITY LEDGER (Phase 3A, user directive, 2026-08-23).

    One row per REAL attempt made by
    app.execution.live_arbitrage_executor.LiveArbitrageExecutor — this
    table stays EMPTY until LIVE_TRADING_ENABLED is explicitly flipped
    True by an operator (never by this codebase) AND a specific
    execute_one_arbitrage() call is made (never automatic). Records
    predicted vs ACTUAL outcome — actual fill prices/quantities/fees come
    directly from what Binance/Bybit returned for that order, never from
    the paper engine. Written by
    app.database.repository.save_live_arbitrage_execution."""

    __tablename__ = "live_arbitrage_executions"
    __table_args__ = (Index("ix_live_arbitrage_executions_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    attempt_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), unique=True, index=True)
    symbol: Mapped[str]
    outcome: Mapped[str]
    reason: Mapped[str | None]
    started_at: Mapped[datetime]
    completed_at: Mapped[datetime | None]

    predicted_net_profit_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))
    predicted_fees_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))
    predicted_slippage_pct: Mapped[float | None] = mapped_column(Numeric(12, 6))
    safety_adjusted_predicted_profit_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))

    buy_client_order_id: Mapped[str | None]
    buy_exchange_order_id: Mapped[str | None]
    buy_status: Mapped[str | None]
    buy_filled_qty: Mapped[float] = mapped_column(Numeric(30, 6))
    buy_avg_fill_price: Mapped[float | None] = mapped_column(Numeric(30, 12))
    buy_fees_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))
    buy_latency_ms: Mapped[float | None] = mapped_column(Numeric(14, 3))

    sell_client_order_id: Mapped[str | None]
    sell_exchange_order_id: Mapped[str | None]
    sell_status: Mapped[str | None]
    sell_filled_qty: Mapped[float] = mapped_column(Numeric(30, 6))
    sell_avg_fill_price: Mapped[float | None] = mapped_column(Numeric(30, 12))
    sell_fees_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))
    sell_latency_ms: Mapped[float | None] = mapped_column(Numeric(14, 3))

    neutralization_order_id: Mapped[str | None]
    neutralization_filled_qty: Mapped[float] = mapped_column(Numeric(30, 6))

    actual_realized_spread_pct: Mapped[float | None] = mapped_column(Numeric(12, 6))
    actual_net_pnl_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))
    prediction_error_usd: Mapped[float | None] = mapped_column(Numeric(20, 6))

    created_at: Mapped[datetime] = mapped_column(server_default=func.now())
