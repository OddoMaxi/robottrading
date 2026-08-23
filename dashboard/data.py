"""Data-fetching layer shared by Simple and Expert mode.

Every function here opens its own short-lived async engine — Streamlit
calls asyncio.run() fresh on every rerun, giving each run its own event
loop, and asyncpg connections can't be reused across event loops (unlike
the FastAPI app's long-lived engine).
"""

import asyncio
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

import aiohttp
import pandas as pd
import streamlit as st
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config.settings import get_settings
from app.database.models import OpportunityRecord, PriceSnapshot, VirtualPortfolioRecord
from app.reporting.benchmark import BenchmarkReport, build_benchmark_report
from app.reporting.daily import DailySummary, build_daily_summary
from app.reporting.dex_execution_funnel import DexStrategyFunnel, build_dex_execution_funnel
from app.reporting.dex_reality import DexRealityCaptureReport, build_dex_reality_capture
from app.reporting.execution_funnel import ExecutionFunnelReport, build_execution_funnel
from app.reporting.master_funnel import MasterFrequencyReport, build_master_frequency_report
from app.reporting.holding_time_performance import (
    HoldingTimeBucketStats,
    HoldingTimeDistribution,
    build_holding_time_distribution,
    build_holding_time_performance,
)
from app.reporting.data_quality import DataQualityReport, build_data_quality_report
from app.reporting.dex_capital_tier_replay import CapitalTierReplayResult, fetch_deduplicated_opportunities_since, replay_across_tiers
from app.reporting.dex_stress_test import ALL_SCENARIOS, StressScenarioResult, fetch_dex_cross_opportunities_with_price_snapshot, simulate_stress_scenario
from app.reporting.duplicate_monitor import DuplicateMonitorReport, build_duplicate_monitor_report
from app.reporting.global_capital import GlobalCapitalState, build_global_capital_state
from app.reporting.global_rejection_breakdown import RejectionReasonRow, build_global_rejection_breakdown
from app.reporting.master_strategy_ranking import StrategyPerformance, build_master_strategy_ranking
from app.reporting.reality_baseline import PRE_PHASE_2_VALIDATION_BASELINE_AT, REALITY_BASELINE_AT, hours_since_baseline, window_contains_pre_baseline_data
from app.reporting.reality_reliability import RealityReliabilityReport, build_reality_reliability_report
from app.reporting.rotation import RotationReport, build_rotation_report
from app.reporting.cex_scan_shadow_report import (
    CexScanAgreementBreakdown,
    CexScanDisagreementRow,
    build_cex_scan_agreement_breakdown,
    build_cex_scan_disagreement_breakdown,
)
from app.reporting.shadow_report import (
    ShadowEngineBreakdown,
    ShadowRecentDecision,
    ShadowStrategyBreakdown,
    ShadowSummary,
    build_shadow_engine_breakdown,
    build_shadow_strategy_breakdown,
    build_shadow_summary,
    list_recent_shadow_decisions,
)
from app.reporting.simple_summary import (
    CapitalUtilization,
    EquityPoint,
    OpenPosition,
    PerformanceMetrics,
    RealityCaptureReport,
    RobotStatus,
    TradeRow,
    TradeStatusBreakdown,
    build_capital_utilization,
    build_equity_curve,
    build_performance_metrics,
    build_portfolio_capital,
    build_reality_capture,
    build_robot_status,
    build_trade_status_breakdown,
    list_open_positions,
    list_recent_trades,
)
from app.reporting.weekly import WeeklyAnalytics, build_weekly_analytics
from app.reporting.why_no_trade import WhyNoTradeReport, build_why_no_trade_report
from dashboard.theme import EXECUTION_MODE_LABELS, STRATEGY_LABELS, ILLUSTRATIVE_CAPITAL_USD, humanize_delta

# Reference portfolio for every single-portfolio KPI (Simple Mode's capital
# card, the Fast Rotation section, etc.) — matches the Fast-Rotation spec's
# own worked examples (section 6-9, 58) and Simple Mode's own mockup, both
# of which use $5,000.
ROTATION_REFERENCE_PORTFOLIO = "5K"

# price_snapshots grows fast (event-driven detection writes a tick almost
# every scan). Charting only needs enough points to fill 1-15min candles —
# capping the row count bounds worst-case query time, pandas memory, and
# resample cost regardless of how large the table gets or how long a
# lookback window is requested.
MAX_PRICE_HISTORY_ROWS = 20_000
MAX_BID_ASK_HISTORY_ROWS = 150_000


async def fetch_opportunities(limit: int = 300) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(OpportunityRecord).order_by(OpportunityRecord.detected_at.desc()).limit(limit)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    result_df = pd.DataFrame(
        [
            {
                "Stratégie": STRATEGY_LABELS.get(r.strategy, r.strategy),
                "_strategy": r.strategy,
                "Paire": r.symbol,
                "Gain brut (%)": float(r.gross_spread_pct),
                "Seuil de rentabilité (%)": float(r.break_even_pct) if r.break_even_pct is not None else None,
                "Gain net (%)": float(r.net_spread_pct) if r.net_spread_pct is not None else None,
                "Résultat sur 1000 $": (float(r.net_spread_pct) / 100 * ILLUSTRATIVE_CAPITAL_USD)
                if r.net_spread_pct is not None
                else None,
                "Meilleure exécution": EXECUTION_MODE_LABELS.get(r.execution_mode, "—"),
                "Proba. exécution": float(r.execution_fill_probability) * 100 if r.execution_fill_probability is not None else None,
                "Détecté": humanize_delta(r.detected_at),
                "_detected_at": r.detected_at,
                "_capital_usd": float(r.capital_usd) if r.capital_usd is not None else None,
                "_expected_profit_usd": float(r.expected_profit_usd) if r.expected_profit_usd is not None else None,
                "_gross_spread_pct": float(r.gross_spread_pct),
                "_break_even_pct": float(r.break_even_pct) if r.break_even_pct is not None else None,
                "_holding_period_seconds": float(r.holding_period_seconds) if r.holding_period_seconds is not None else None,
                "_execution_fill_probability": float(r.execution_fill_probability) if r.execution_fill_probability is not None else None,
                "_classification": r.classification,
                "_status": r.status,
                "_legs": r.legs,
                "_theoretical_edge_pct": float(r.theoretical_edge_pct) if r.theoretical_edge_pct is not None else None,
                "_depth_adjusted_edge_pct": float(r.depth_adjusted_edge_pct) if r.depth_adjusted_edge_pct is not None else None,
                "_realistic_executable_edge_pct": float(r.realistic_executable_edge_pct) if r.realistic_executable_edge_pct is not None else None,
                "_optimal_capital_usd": float(r.optimal_capital_usd) if r.optimal_capital_usd is not None else None,
                "_max_profitable_capital_usd": float(r.max_profitable_capital_usd) if r.max_profitable_capital_usd is not None else None,
            }
            for r in rows
        ]
    )
    # Some columns (break-even, execution probability) are only computed for
    # a subset of strategies — if a display window happens to contain none
    # of those rows, pandas infers the column as `object` dtype from an
    # all-None list instead of float64, and Styler's numeric format string
    # then breaks on the raw `None` (unlike NaN, which it handles fine).
    # Forcing numeric dtype here converts None -> NaN regardless.
    if not result_df.empty:
        numeric_columns = ["Gain brut (%)", "Seuil de rentabilité (%)", "Gain net (%)", "Résultat sur 1000 $", "Proba. exécution"]
        result_df[numeric_columns] = result_df[numeric_columns].apply(pd.to_numeric, errors="coerce")
    return result_df


@st.cache_data(ttl=5, show_spinner=False)
def get_opportunities_cached(limit: int = 300) -> pd.DataFrame:
    return asyncio.run(fetch_opportunities(limit))


async def fetch_last_profitable_spike() -> dict | None:
    """Most recent opportunity that was genuinely profitable after fees, plus how often that's happened lately.

    Queried directly (not from the already-fetched recent-opportunities
    table) because that table is capped at a few hundred rows and, since
    detection went event-driven, that can now be just the last few seconds
    — too short a window to find a rare profitable spike in.
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(OpportunityRecord)
                .where(OpportunityRecord.net_spread_pct > 0)
                .order_by(OpportunityRecord.detected_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            cutoff = (datetime.now(UTC) - timedelta(hours=24)).replace(tzinfo=None)
            count_result = await session.execute(
                select(func.count()).where(OpportunityRecord.net_spread_pct > 0, OpportunityRecord.detected_at >= cutoff)
            )
            count_24h = count_result.scalar()
    finally:
        await engine.dispose()
    return {
        "symbol": row.symbol,
        "strategy": STRATEGY_LABELS.get(row.strategy, row.strategy),
        "net_spread_pct": float(row.net_spread_pct),
        "detected_at": row.detected_at,
        "count_24h": count_24h,
    }


@st.cache_data(ttl=15, show_spinner=False)
def get_last_profitable_spike_cached() -> dict | None:
    return asyncio.run(fetch_last_profitable_spike())


async def fetch_price_history(symbol: str, hours: float = 3.0) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        # recorded_at is stored as a naive UTC timestamp (server_default=func.now(),
        # no timezone=True) — strip tzinfo so asyncpg isn't asked to compare
        # a naive column against an aware bind parameter.
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
        async with session_factory() as session:
            result = await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.symbol == symbol, PriceSnapshot.recorded_at >= cutoff)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(MAX_PRICE_HISTORY_ROWS)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    history_df = pd.DataFrame(
        [
            {
                "exchange": r.exchange,
                "recorded_at": r.recorded_at,
                "mid": (float(r.bid) + float(r.ask)) / 2,
            }
            for r in rows
        ]
    )
    return history_df.sort_values("recorded_at") if not history_df.empty else history_df


async def fetch_bid_ask_history(symbols: list[str], hours: float = 2.0) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
        async with session_factory() as session:
            result = await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.symbol.in_(symbols), PriceSnapshot.recorded_at >= cutoff)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(MAX_BID_ASK_HISTORY_ROWS)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    bid_ask_df = pd.DataFrame(
        [
            {"symbol": r.symbol, "exchange": r.exchange, "recorded_at": r.recorded_at, "bid": float(r.bid), "ask": float(r.ask)}
            for r in rows
        ]
    )
    return bid_ask_df.sort_values("recorded_at") if not bid_ask_df.empty else bid_ask_df


@st.cache_data(ttl=30, show_spinner=False)
def get_bid_ask_history_cached(symbols: tuple[str, ...], hours: float) -> pd.DataFrame:
    return asyncio.run(fetch_bid_ask_history(list(symbols), hours))


async def fetch_daily_summary() -> DailySummary:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_daily_summary(session)
    finally:
        await engine.dispose()


async def fetch_weekly_analytics() -> WeeklyAnalytics:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_weekly_analytics(session)
    finally:
        await engine.dispose()


@st.cache_data(ttl=10, show_spinner=False)
def get_daily_summary_cached() -> DailySummary:
    return asyncio.run(fetch_daily_summary())


@st.cache_data(ttl=300, show_spinner=False)
def get_weekly_analytics_cached() -> WeeklyAnalytics:
    return asyncio.run(fetch_weekly_analytics())


async def fetch_rotation_report(mode: str | None, hours: float = 24.0) -> RotationReport | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(VirtualPortfolioRecord).where(VirtualPortfolioRecord.name == ROTATION_REFERENCE_PORTFOLIO)
            )
            portfolio = result.scalar_one_or_none()
            if portfolio is None:
                return None
            return await build_rotation_report(
                session, portfolio.id, portfolio.name, float(portfolio.initial_capital_usd), mode=mode, hours=hours
            )
    finally:
        await engine.dispose()


@st.cache_data(ttl=10, show_spinner=False)
def get_rotation_report_cached(mode: str | None, hours: float = 24.0) -> RotationReport | None:
    return asyncio.run(fetch_rotation_report(mode, hours))


async def fetch_holding_time_performance(hours: float = 24.0) -> list[HoldingTimeBucketStats]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(VirtualPortfolioRecord).where(VirtualPortfolioRecord.name == ROTATION_REFERENCE_PORTFOLIO)
            )
            portfolio = result.scalar_one_or_none()
            if portfolio is None:
                return []
            return await build_holding_time_performance(session, portfolio.id, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=60, show_spinner=False)
def get_holding_time_performance_cached(hours: float = 24.0) -> list[HoldingTimeBucketStats]:
    return asyncio.run(fetch_holding_time_performance(hours))


async def fetch_holding_time_distribution(hours: float = 24.0) -> HoldingTimeDistribution | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            return await build_holding_time_distribution(session, portfolio.id, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=60, show_spinner=False)
def get_holding_time_distribution_cached(hours: float = 24.0) -> HoldingTimeDistribution | None:
    return asyncio.run(fetch_holding_time_distribution(hours))


# --- Simple Mode data ---


async def _get_reference_portfolio(session) -> VirtualPortfolioRecord | None:
    result = await session.execute(select(VirtualPortfolioRecord).where(VirtualPortfolioRecord.name == ROTATION_REFERENCE_PORTFOLIO))
    return result.scalar_one_or_none()


async def fetch_robot_status() -> RobotStatus:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_robot_status(session)
    finally:
        await engine.dispose()


@st.cache_data(ttl=3, show_spinner=False)
def get_robot_status_cached() -> RobotStatus:
    return asyncio.run(fetch_robot_status())


async def fetch_simple_capital() -> float | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            return await build_portfolio_capital(session, portfolio.id, float(portfolio.initial_capital_usd))
    finally:
        await engine.dispose()


@st.cache_data(ttl=5, show_spinner=False)
def get_simple_capital_cached() -> float | None:
    return asyncio.run(fetch_simple_capital())


async def fetch_equity_curve(hours: float | None = 24.0) -> list[EquityPoint]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return []
            return await build_equity_curve(session, portfolio.id, float(portfolio.initial_capital_usd), hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=10, show_spinner=False)
def get_equity_curve_cached(hours: float | None = 24.0) -> list[EquityPoint]:
    return asyncio.run(fetch_equity_curve(hours))


async def fetch_recent_trades(limit: int = 50) -> list[TradeRow]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return []
            return await list_recent_trades(session, portfolio.id, limit=limit)
    finally:
        await engine.dispose()


@st.cache_data(ttl=5, show_spinner=False)
def get_recent_trades_cached(limit: int = 50) -> list[TradeRow]:
    return asyncio.run(fetch_recent_trades(limit))


async def fetch_capital_utilization() -> CapitalUtilization | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            total_capital = await build_portfolio_capital(session, portfolio.id, float(portfolio.initial_capital_usd))
            return await build_capital_utilization(session, portfolio.id, total_capital)
    finally:
        await engine.dispose()


@st.cache_data(ttl=5, show_spinner=False)
def get_capital_utilization_cached() -> CapitalUtilization | None:
    return asyncio.run(fetch_capital_utilization())


async def fetch_open_positions() -> list[OpenPosition]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return []
            total_capital = await build_portfolio_capital(session, portfolio.id, float(portfolio.initial_capital_usd))
            return await list_open_positions(session, portfolio.id, total_capital)
    finally:
        await engine.dispose()


@st.cache_data(ttl=5, show_spinner=False)
def get_open_positions_cached() -> list[OpenPosition]:
    return asyncio.run(fetch_open_positions())






async def fetch_trade_status_breakdown(hours: float = 24.0) -> TradeStatusBreakdown | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            return await build_trade_status_breakdown(session, portfolio.id, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_trade_status_breakdown_cached(hours: float = 24.0) -> TradeStatusBreakdown | None:
    return asyncio.run(fetch_trade_status_breakdown(hours))


async def fetch_reality_capture(hours: float = 24.0) -> RealityCaptureReport | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            return await build_reality_capture(session, portfolio.id, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_reality_capture_cached(hours: float = 24.0) -> RealityCaptureReport | None:
    return asyncio.run(fetch_reality_capture(hours))


async def fetch_performance_metrics(hours: float = 24.0) -> PerformanceMetrics | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            return await build_performance_metrics(session, portfolio.id, float(portfolio.initial_capital_usd), hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=30, show_spinner=False)
def get_performance_metrics_cached(hours: float = 24.0) -> PerformanceMetrics | None:
    return asyncio.run(fetch_performance_metrics(hours))


@dataclass(slots=True)
class ReadinessSummary:
    verdict: str | None  # None = engine API unreachable, not "not ready"
    checks: list[dict] = field(default_factory=list)


async def fetch_micro_live_readiness() -> ReadinessSummary:
    """Reality Engine spec, sections 59-60 — HTTP GET to the engine's own
    FastAPI app rather than the database directly: the readiness verdict
    depends on live in-process state (kill switch, live capital pool) and a
    network check (Binance Testnet ping) that only the engine process has
    access to, unlike every other fetch_* in this module. Never raises —
    an unreachable engine API reports verdict=None rather than crashing the
    dashboard, matching the read-only, must-never-crash rule every other
    fetch here already follows for a missing/stale database."""
    base_url = get_settings().engine_api_base_url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/micro-live/readiness", timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                payload = await response.json()
        return ReadinessSummary(verdict=payload["verdict"], checks=payload["checks"])
    except Exception:
        return ReadinessSummary(verdict=None, checks=[])


@st.cache_data(ttl=30, show_spinner=False)
def get_micro_live_readiness_cached() -> ReadinessSummary:
    return asyncio.run(fetch_micro_live_readiness())


@dataclass(slots=True)
class MasterStatusSummary:
    reachable: bool
    paper_authority_enabled: bool = False
    rollback_reason: str | None = None
    rollback_at: float | None = None
    total_capital_usd: float = 0.0
    realized_pnl_usd: float = 0.0
    available_capital_usd: float = 0.0
    reserved_capital_usd: float = 0.0
    reserved_cex_usd: float = 0.0
    reserved_dex_usd: float = 0.0
    invariant_violations: list = field(default_factory=list)
    grants_count: int = 0
    rejections_count: int = 0
    fills_count: int = 0
    real_orders_placed: bool = False


async def fetch_master_status() -> MasterStatusSummary:
    """PHASE 2C (user directive, 2026-08-23) — same HTTP-to-the-engine
    pattern as fetch_micro_live_readiness: MASTER's live capital state is
    in-process state only the engine itself has, not something a DB query
    can reconstruct. Never raises — an unreachable engine reports
    reachable=False rather than crashing the dashboard."""
    base_url = get_settings().engine_api_base_url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/master/status", timeout=aiohttp.ClientTimeout(total=10)) as response:
                response.raise_for_status()
                payload = await response.json()
        return MasterStatusSummary(reachable=True, **payload)
    except Exception:
        return MasterStatusSummary(reachable=False)


@st.cache_data(ttl=5, show_spinner=False)
def get_master_status_cached() -> MasterStatusSummary:
    return asyncio.run(fetch_master_status())


@dataclass(slots=True)
class MicroLiveBinanceReadiness:
    """PHASE 2D (user directive, 2026-08-23) — MICRO-LIVE READINESS.
    Never carries a raw credential; every field here is either a public
    market fact, a real (but non-secret) account balance/status, or a
    session-level dry-run counter. real_orders_placed is always 0."""

    reachable: bool
    binance_connectivity: bool = False
    credentials_configured: bool = False
    latency_ms: float | None = None
    connectivity_detail: str | None = None
    account_mode: str = "mainnet_read_only"
    live_trading_enabled: bool = False
    real_balance_usdt: float | None = None
    real_balance_verified: bool = False
    account_snapshot_error: str | None = None
    can_trade: bool | None = None
    can_withdraw: bool | None = None  # account-level KYC flag — see key_enable_withdrawals for the key's own permission scope
    key_enable_reading: bool | None = None
    key_enable_withdrawals: bool | None = None  # the field item 2's "no withdrawal permission" requirement is actually checked against
    key_enable_spot_and_margin_trading: bool | None = None
    key_ip_restricted: bool | None = None
    api_restrictions_error: str | None = None
    micro_live_cap_usdt: float = 10.0
    max_live_capital_usdt: float = 10.0
    paper_capital_usd: float = 10_000.0
    opportunities_observed: int = 0
    executable_with_cap: int = 0
    non_executable: int = 0
    rejection_reasons: dict = field(default_factory=dict)
    avg_estimated_fees_usd: float | None = None
    avg_estimated_slippage_pct: float | None = None
    avg_net_profit_after_real_constraints_usd: float | None = None
    live_kill_switch_engaged: bool = False
    real_orders_placed: int = 0


async def fetch_micro_live_binance_readiness() -> MicroLiveBinanceReadiness:
    """Same HTTP-to-the-engine pattern as fetch_master_status: real
    Binance connectivity/account state and the session's dry-run counters
    live only in the engine process (app.execution.micro_live), not in
    the database. Never raises — an unreachable engine reports
    reachable=False rather than crashing the dashboard."""
    base_url = get_settings().engine_api_base_url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url}/micro-live/binance-readiness", timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        return MicroLiveBinanceReadiness(reachable=True, **payload)
    except Exception:
        return MicroLiveBinanceReadiness(reachable=False)


@st.cache_data(ttl=5, show_spinner=False)
def get_micro_live_binance_readiness_cached() -> MicroLiveBinanceReadiness:
    return asyncio.run(fetch_micro_live_binance_readiness())


@dataclass(slots=True)
class DistributionSummary:
    count: int = 0
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    positive_rate_pct: float | None = None
    negative_rate_pct: float | None = None
    worst: float | None = None
    best: float | None = None
    stdev: float | None = None


@dataclass(slots=True)
class GroupEdgeSummary:
    key: str
    observations: int
    net_profit: DistributionSummary
    gross_profit_mean: float | None
    avg_fees_usd: float | None
    avg_slippage_pct: float | None
    positive_net_rate_pct: float | None
    real_fee_coverage_pct: float | None


@dataclass(slots=True)
class LuncEdgeSummary:
    observations: int
    net_profit: DistributionSummary
    avg_book_spread_pct: float | None
    avg_available_depth_usd: float | None
    avg_slippage_pct: float | None
    min_notional_pass_rate_pct: float | None
    lot_size_pass_rate_pct: float | None
    positive_net_rate_pct: float | None
    real_fee_coverage_pct: float | None


@dataclass(slots=True)
class MicroLiveEdgeSummary:
    reachable: bool
    observations: int = 0
    real_fee_coverage_pct: float | None = None
    gross_profit: DistributionSummary = field(default_factory=DistributionSummary)
    net_profit: DistributionSummary = field(default_factory=DistributionSummary)
    net_return_bps: DistributionSummary = field(default_factory=DistributionSummary)
    avg_fees_usd: float | None = None
    avg_slippage_pct: float | None = None
    rejection_reasons: dict = field(default_factory=dict)
    top_positive_symbols: list[GroupEdgeSummary] = field(default_factory=list)
    negative_symbols: list[GroupEdgeSummary] = field(default_factory=list)
    lunc_usdt: LuncEdgeSummary | None = None
    recommended_safety_margin_usd: float = 0.0
    qualifying_after_gate: int = 0
    real_orders_placed: int = 0


def _parse_distribution(payload: dict) -> DistributionSummary:
    return DistributionSummary(**payload)


def _parse_group(payload: dict) -> GroupEdgeSummary:
    return GroupEdgeSummary(
        key=payload["key"],
        observations=payload["observations"],
        net_profit=_parse_distribution(payload["net_profit"]),
        gross_profit_mean=payload["gross_profit_mean"],
        avg_fees_usd=payload["avg_fees_usd"],
        avg_slippage_pct=payload["avg_slippage_pct"],
        positive_net_rate_pct=payload["positive_net_rate_pct"],
        real_fee_coverage_pct=payload["real_fee_coverage_pct"],
    )


async def fetch_micro_live_edge_report(hours: float = 72.0) -> MicroLiveEdgeSummary:
    """PHASE 2E — REAL EDGE VALIDATION (user directive, 2026-08-23). Same
    HTTP-to-the-engine pattern as the other Phase 2C/2D fetchers, reading
    the persisted micro_live_observations analysis. Never raises."""
    base_url = get_settings().engine_api_base_url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{base_url}/micro-live/edge-report", params={"hours": hours}, timeout=aiohttp.ClientTimeout(total=15)
            ) as response:
                response.raise_for_status()
                payload = await response.json()
        lunc_payload = payload.get("lunc_usdt")
        return MicroLiveEdgeSummary(
            reachable=True,
            observations=payload["observations"],
            real_fee_coverage_pct=payload["real_fee_coverage_pct"],
            gross_profit=_parse_distribution(payload["gross_profit"]),
            net_profit=_parse_distribution(payload["net_profit"]),
            net_return_bps=_parse_distribution(payload["net_return_bps"]),
            avg_fees_usd=payload["avg_fees_usd"],
            avg_slippage_pct=payload["avg_slippage_pct"],
            rejection_reasons=payload["rejection_reasons"],
            top_positive_symbols=[_parse_group(g) for g in payload["top_positive_symbols"]],
            negative_symbols=[_parse_group(g) for g in payload["negative_symbols"]],
            lunc_usdt=(
                LuncEdgeSummary(
                    observations=lunc_payload["observations"],
                    net_profit=_parse_distribution(lunc_payload["net_profit"]),
                    avg_book_spread_pct=lunc_payload["avg_book_spread_pct"],
                    avg_available_depth_usd=lunc_payload["avg_available_depth_usd"],
                    avg_slippage_pct=lunc_payload["avg_slippage_pct"],
                    min_notional_pass_rate_pct=lunc_payload["min_notional_pass_rate_pct"],
                    lot_size_pass_rate_pct=lunc_payload["lot_size_pass_rate_pct"],
                    positive_net_rate_pct=lunc_payload["positive_net_rate_pct"],
                    real_fee_coverage_pct=lunc_payload["real_fee_coverage_pct"],
                )
                if lunc_payload is not None
                else None
            ),
            recommended_safety_margin_usd=payload["recommended_safety_margin_usd"],
            qualifying_after_gate=payload["qualifying_after_gate"],
            real_orders_placed=payload["real_orders_placed"],
        )
    except Exception:
        return MicroLiveEdgeSummary(reachable=False)


@st.cache_data(ttl=15, show_spinner=False)
def get_micro_live_edge_report_cached(hours: float = 72.0) -> MicroLiveEdgeSummary:
    return asyncio.run(fetch_micro_live_edge_report(hours=hours))


@dataclass(slots=True)
class RealTradeSummary:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    outcome: str
    actual_net_pnl_usd: float | None
    started_at: str


@dataclass(slots=True)
class RealTradingDashboardSummary:
    reachable: bool
    total_real_capital_target_usdt: float = 0.0
    binance_target_capital_usdt: float = 0.0
    bybit_target_capital_usdt: float = 0.0
    binance_balance_usdt: float = 0.0
    bybit_balance_usdt: float = 0.0
    available_capital_usdt: float = 0.0
    today_real_pnl_usd: float = 0.0
    total_real_pnl_usd: float = 0.0
    real_trades: int = 0
    wins: int = 0
    losses: int = 0
    win_rate_pct: float | None = None
    total_real_fees_usd: float = 0.0
    average_profit_per_trade_usd: float | None = None
    current_best_opportunity: dict | None = None
    active_orders: int = 0
    last_trades: list[RealTradeSummary] = field(default_factory=list)
    kill_switch_engaged: bool = False
    kill_switch_reason: str | None = None
    live_trading_enabled: bool = False
    real_orders_placed: int = 0


async def fetch_live_dashboard_summary() -> RealTradingDashboardSummary:
    """PHASE 3, item 10 (user directive, 2026-08-23) — same HTTP-to-the-
    engine pattern as every other live-state fetcher here. Never raises —
    an unreachable engine reports reachable=False."""
    base_url = get_settings().engine_api_base_url
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(f"{base_url}/live/dashboard-summary", timeout=aiohttp.ClientTimeout(total=15)) as response:
                response.raise_for_status()
                payload = await response.json()
        last_trades = [
            RealTradeSummary(
                symbol=t["symbol"], buy_exchange=t["buy_exchange"], sell_exchange=t["sell_exchange"],
                outcome=t["outcome"], actual_net_pnl_usd=t["actual_net_pnl_usd"], started_at=t["started_at"],
            )
            for t in payload.get("last_trades", [])
        ]
        return RealTradingDashboardSummary(
            reachable=True,
            total_real_capital_target_usdt=payload["total_real_capital_target_usdt"],
            binance_target_capital_usdt=payload["binance_target_capital_usdt"],
            bybit_target_capital_usdt=payload["bybit_target_capital_usdt"],
            binance_balance_usdt=payload["binance_balance_usdt"],
            bybit_balance_usdt=payload["bybit_balance_usdt"],
            available_capital_usdt=payload["available_capital_usdt"],
            today_real_pnl_usd=payload["today_real_pnl_usd"],
            total_real_pnl_usd=payload["total_real_pnl_usd"],
            real_trades=payload["real_trades"],
            wins=payload["wins"],
            losses=payload["losses"],
            win_rate_pct=payload["win_rate_pct"],
            total_real_fees_usd=payload["total_real_fees_usd"],
            average_profit_per_trade_usd=payload["average_profit_per_trade_usd"],
            current_best_opportunity=payload["current_best_opportunity"],
            active_orders=payload["active_orders"],
            last_trades=last_trades,
            kill_switch_engaged=payload["kill_switch_engaged"],
            kill_switch_reason=payload["kill_switch_reason"],
            live_trading_enabled=payload["live_trading_enabled"],
            real_orders_placed=payload["real_orders_placed"],
        )
    except Exception:
        return RealTradingDashboardSummary(reachable=False)


@st.cache_data(ttl=5, show_spinner=False)
def get_live_dashboard_summary_cached() -> RealTradingDashboardSummary:
    return asyncio.run(fetch_live_dashboard_summary())


async def fetch_execution_funnel(hours: float = 24.0) -> ExecutionFunnelReport:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_execution_funnel(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=30, show_spinner=False)
def get_execution_funnel_cached(hours: float = 24.0) -> ExecutionFunnelReport:
    return asyncio.run(fetch_execution_funnel(hours))


async def fetch_why_no_trade() -> WhyNoTradeReport | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            total_capital = await build_portfolio_capital(session, portfolio.id, float(portfolio.initial_capital_usd))
            return await build_why_no_trade_report(session, portfolio.id, total_capital)
    finally:
        await engine.dispose()


@st.cache_data(ttl=10, show_spinner=False)
def get_why_no_trade_cached() -> WhyNoTradeReport | None:
    return asyncio.run(fetch_why_no_trade())


async def fetch_master_frequency_report(hours: float = 24.0) -> MasterFrequencyReport:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_master_frequency_report(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=30, show_spinner=False)
def get_master_frequency_report_cached(hours: float = 24.0) -> MasterFrequencyReport:
    return asyncio.run(fetch_master_frequency_report(hours))


async def fetch_dex_reality_capture(hours: float = 24.0) -> list[DexRealityCaptureReport]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_dex_reality_capture(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=30, show_spinner=False)
def get_dex_reality_capture_cached(hours: float = 24.0) -> list[DexRealityCaptureReport]:
    return asyncio.run(fetch_dex_reality_capture(hours))


async def fetch_benchmark_report(hours: float = 24.0) -> BenchmarkReport:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_benchmark_report(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=60, show_spinner=False)
def get_benchmark_report_cached(hours: float = 24.0) -> BenchmarkReport:
    return asyncio.run(fetch_benchmark_report(hours))


async def fetch_dex_execution_funnel(hours: float = 24.0) -> list[DexStrategyFunnel]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_dex_execution_funnel(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_dex_execution_funnel_cached(hours: float = 24.0) -> list[DexStrategyFunnel]:
    return asyncio.run(fetch_dex_execution_funnel(hours))


# --- Reality Dashboard (V5/V5.5 Master Orchestration, user directive, 2026-08-22) ---

DEX_PAPER_TRADING_CAPITAL_USD = 5_000.0  # mirrors main.py / app.reporting.global_capital — see that module's docstring


async def fetch_global_capital_state() -> GlobalCapitalState | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            portfolio = await _get_reference_portfolio(session)
            if portfolio is None:
                return None
            return await build_global_capital_state(session, portfolio.id, float(portfolio.initial_capital_usd), DEX_PAPER_TRADING_CAPITAL_USD)
    finally:
        await engine.dispose()


@st.cache_data(ttl=5, show_spinner=False)
def get_global_capital_state_cached() -> GlobalCapitalState | None:
    return asyncio.run(fetch_global_capital_state())


async def fetch_master_strategy_ranking(hours: float = 24.0) -> list[StrategyPerformance]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_master_strategy_ranking(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_master_strategy_ranking_cached(hours: float = 24.0) -> list[StrategyPerformance]:
    return asyncio.run(fetch_master_strategy_ranking(hours))


async def fetch_duplicate_monitor_report() -> DuplicateMonitorReport:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_duplicate_monitor_report(session)
    finally:
        await engine.dispose()


@st.cache_data(ttl=30, show_spinner=False)
def get_duplicate_monitor_report_cached() -> DuplicateMonitorReport:
    return asyncio.run(fetch_duplicate_monitor_report())


async def fetch_global_rejection_breakdown(hours: float = 24.0) -> list[RejectionReasonRow]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_global_rejection_breakdown(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_global_rejection_breakdown_cached(hours: float = 24.0) -> list[RejectionReasonRow]:
    return asyncio.run(fetch_global_rejection_breakdown(hours))


async def fetch_data_quality_report() -> DataQualityReport:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_data_quality_report(session)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_data_quality_report_cached() -> DataQualityReport:
    return asyncio.run(fetch_data_quality_report())


async def fetch_reality_reliability_report() -> RealityReliabilityReport:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_reality_reliability_report(session)
    finally:
        await engine.dispose()


@st.cache_data(ttl=60, show_spinner=False)
def get_reality_reliability_report_cached() -> RealityReliabilityReport:
    return asyncio.run(fetch_reality_reliability_report())


DEX_GAS_BY_CHAIN_USD = {"solana": 0.005, "eth": 5.0, "bsc": 0.30}


async def fetch_stress_test_results() -> list[StressScenarioResult]:
    """Live-computed against whatever dex_cross opportunities exist since
    the Reality Baseline — never the audit's original one-off manual run
    hardcoded (spec Part AD's own rule)."""
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            opps = await fetch_dex_cross_opportunities_with_price_snapshot(session, REALITY_BASELINE_AT)
            if not opps:
                return []
            return [simulate_stress_scenario(opps, scenario, base_gas_cost_usd=0.005, rng=random.Random(2026)) for scenario in ALL_SCENARIOS]
    finally:
        await engine.dispose()


@st.cache_data(ttl=300, show_spinner=False)
def get_stress_test_results_cached() -> list[StressScenarioResult]:
    return asyncio.run(fetch_stress_test_results())


async def fetch_capital_tier_replay_results() -> list[CapitalTierReplayResult]:
    """Live-computed against every deduplicated opportunity since the
    Reality Baseline, replayed through the real, fixed attempt_dex_trade
    pipeline — never hardcoded (spec Part AC's own rule)."""
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            opps = await fetch_deduplicated_opportunities_since(session, REALITY_BASELINE_AT)
            if not opps:
                return []
            return replay_across_tiers(opps, DEX_GAS_BY_CHAIN_USD, default_gas_cost_usd=1.0, rng_factory=lambda: random.Random(2026))
    finally:
        await engine.dispose()


@st.cache_data(ttl=300, show_spinner=False)
def get_capital_tier_replay_results_cached() -> list[CapitalTierReplayResult]:
    return asyncio.run(fetch_capital_tier_replay_results())


# --- Phase 2 — Global Orchestration, SHADOW MODE ONLY (user directive, 2026-08-22) ---


async def fetch_shadow_summary(hours: float = 24.0) -> ShadowSummary:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_shadow_summary(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_shadow_summary_cached(hours: float = 24.0) -> ShadowSummary:
    return asyncio.run(fetch_shadow_summary(hours))


async def fetch_shadow_engine_breakdown(hours: float = 24.0) -> list[ShadowEngineBreakdown]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_shadow_engine_breakdown(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_shadow_engine_breakdown_cached(hours: float = 24.0) -> list[ShadowEngineBreakdown]:
    return asyncio.run(fetch_shadow_engine_breakdown(hours))


async def fetch_shadow_strategy_breakdown(hours: float = 24.0) -> list[ShadowStrategyBreakdown]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_shadow_strategy_breakdown(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_shadow_strategy_breakdown_cached(hours: float = 24.0) -> list[ShadowStrategyBreakdown]:
    return asyncio.run(fetch_shadow_strategy_breakdown(hours))


async def fetch_recent_shadow_decisions(limit: int = 15) -> list[ShadowRecentDecision]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await list_recent_shadow_decisions(session, limit=limit)
    finally:
        await engine.dispose()


@st.cache_data(ttl=10, show_spinner=False)
def get_recent_shadow_decisions_cached(limit: int = 15) -> list[ShadowRecentDecision]:
    return asyncio.run(fetch_recent_shadow_decisions(limit))


# --- PHASE 2B — CEX Scan-Level Shadow (user directive, 2026-08-22) ---


async def fetch_cex_scan_agreement_breakdown(hours: float = 24.0) -> CexScanAgreementBreakdown:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_cex_scan_agreement_breakdown(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_cex_scan_agreement_breakdown_cached(hours: float = 24.0) -> CexScanAgreementBreakdown:
    return asyncio.run(fetch_cex_scan_agreement_breakdown(hours))


async def fetch_cex_scan_disagreement_breakdown(hours: float = 24.0) -> list[CexScanDisagreementRow]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_cex_scan_disagreement_breakdown(session, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=15, show_spinner=False)
def get_cex_scan_disagreement_breakdown_cached(hours: float = 24.0) -> list[CexScanDisagreementRow]:
    return asyncio.run(fetch_cex_scan_disagreement_breakdown(hours))
