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
