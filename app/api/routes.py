import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import PRIORITY_EXCHANGES, TRIANGULAR_CROSS_PAIRS, MarketType
from app.config.settings import get_settings
from app.database.models import OpportunityRecord
from app.database.repository import log_system_event
from app.database.session import get_session
from app.execution.live_guard import LiveExecutionRefused, live_guard
from app.execution.live_readiness_gate import build_first_live_gate_report
from app.execution.micro_live import micro_live_orchestrator, micro_live_state
from app.market_data.store import market_data_store
from app.orchestration.control import master_control
from app.orchestration.global_allocator import global_allocator
from app.reporting.dual_leg_edge import DualLegEdgeReport, build_dual_leg_edge_report
from app.reporting.micro_live_edge import DistributionStats, MicroLiveEdgeReport, build_micro_live_edge_report
from app.risk.risk_engine import risk_engine
from app.simulation.live_stress_test import run_live_stress_test

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


class KillSwitchRequest(BaseModel):
    reason: str = "manual"


@router.get("/risk/status")
async def risk_status() -> dict:
    return {
        "kill_switch_engaged": risk_engine.kill_switch_engaged,
        "kill_switch_reason": risk_engine.kill_switch_reason,
        "max_capital_per_trade_usd": risk_engine.limits.max_capital_per_trade_usd,
        "max_concurrent_trades": risk_engine.limits.max_concurrent_trades,
        "max_daily_loss_usd": risk_engine.limits.max_daily_loss_usd,
    }


@router.post("/risk/kill-switch/engage")
async def engage_kill_switch(body: KillSwitchRequest) -> dict:
    """Continuous Execution spec, section 61 — stops all *new* paper
    executions immediately. Detection and observation keep running; only
    capital allocation is halted, and it takes effect on the very next scan."""
    risk_engine.engage_kill_switch(body.reason)
    return {"kill_switch_engaged": True, "reason": body.reason}


@router.post("/risk/kill-switch/disengage")
async def disengage_kill_switch() -> dict:
    risk_engine.disengage_kill_switch()
    return {"kill_switch_engaged": False}


class MasterRollbackRequest(BaseModel):
    reason: str = "manual"


@router.get("/master/status")
async def master_status() -> dict:
    """PHASE 2C (user directive, 2026-08-23) — PAPER TRADING ONLY. Never
    reflects a real order or real capital."""
    now = time.time()
    return {
        "paper_authority_enabled": master_control.paper_authority_enabled,
        "rollback_reason": master_control.rollback_reason,
        "rollback_at": master_control.rollback_at,
        "total_capital_usd": global_allocator.total_capital_usd,
        "realized_pnl_usd": global_allocator.realized_pnl_usd,
        "available_capital_usd": global_allocator.available_capital_usd(now),
        "reserved_capital_usd": global_allocator.locked_capital_usd(now),
        "reserved_cex_usd": global_allocator.locked_by_engine_usd(now, "CEX"),
        "reserved_dex_usd": global_allocator.locked_by_engine_usd(now, "DEX"),
        "invariant_violations": global_allocator.check_invariant(now),
        "grants_count": global_allocator.grants_count,
        "rejections_count": global_allocator.rejections_count,
        "fills_count": global_allocator.fills_count,
        "real_orders_placed": False,
    }


@router.post("/master/rollback")
async def master_rollback(body: MasterRollbackRequest, session: AsyncSession = Depends(get_session)) -> dict:
    """Instant rollback to OLD as sole paper authority — a pure in-memory
    flag flip (app.orchestration.control.master_control), no data
    reconstruction, takes effect on the very next scan cycle for every
    cutover-gated call site in main.py. Persisted to system_events with
    origin="manual" so this is distinguishable from an automatic
    invariant-triggered rollback in the audit trail (Phase 2D, item 1)."""
    previous_state = {"paper_authority_enabled": master_control.paper_authority_enabled}
    master_control.disable(body.reason)
    await log_system_event(
        session,
        event_type="master_rollback",
        severity="warning",
        message=body.reason,
        metadata={
            "origin": "manual",
            "previous_state": previous_state,
            "new_state": {
                "paper_authority_enabled": master_control.paper_authority_enabled,
                "rollback_reason": master_control.rollback_reason,
                "rollback_at": master_control.rollback_at,
            },
        },
    )
    await session.commit()
    return {"paper_authority_enabled": False, "reason": body.reason}


@router.post("/master/enable")
async def master_enable(session: AsyncSession = Depends(get_session)) -> dict:
    """Re-enables MASTER paper authority after a rollback (manual or
    automatic). Also persisted, so the audit trail shows both when
    authority was withdrawn and when it was restored."""
    previous_state = {
        "paper_authority_enabled": master_control.paper_authority_enabled,
        "rollback_reason": master_control.rollback_reason,
        "rollback_at": master_control.rollback_at,
    }
    master_control.enable()
    await log_system_event(
        session,
        event_type="master_enable",
        severity="info",
        message="master paper authority manually re-enabled",
        metadata={
            "origin": "manual",
            "previous_state": previous_state,
            "new_state": {"paper_authority_enabled": master_control.paper_authority_enabled},
        },
    )
    await session.commit()
    return {"paper_authority_enabled": True}


class LiveKillSwitchRequest(BaseModel):
    reason: str = "manual"


@router.get("/live/status")
async def live_status() -> dict:
    """PHASE 2D (user directive, 2026-08-23) — the live execution guard's
    state. live_trading_enabled is read straight from settings (env-var
    default False); this endpoint never exposes credentials."""
    return live_guard.status()


@router.post("/live/kill-switch/engage")
async def engage_live_kill_switch(body: LiveKillSwitchRequest) -> dict:
    live_guard.engage_kill_switch(body.reason)
    return {"kill_switch_engaged": True, "reason": body.reason}


@router.post("/live/kill-switch/disengage")
async def disengage_live_kill_switch() -> dict:
    live_guard.disengage_kill_switch()
    return {"kill_switch_engaged": False}


class LiveExecuteRequest(BaseModel):
    requested_usdt: float


@router.post("/live/execute")
async def live_execute(body: LiveExecuteRequest) -> dict:
    """PHASE 2D, item 7 — this is the ONLY execution-shaped endpoint that
    exists in this codebase, and it places no order: it exists purely to
    prove, mechanically and over real HTTP, that reaching an execution
    path while LIVE_TRADING_ENABLED=false is refused. There is no order-
    placement code below the guard check for it to fall through to."""
    try:
        live_guard.assert_execution_allowed(body.requested_usdt)
    except LiveExecutionRefused as exc:
        return {"executed": False, "real_orders_placed": 0, "refused_reason": str(exc)}
    # Unreachable while live_trading_enabled defaults to False and no
    # explicit authorization step has ever set it to True in this
    # codebase — intentionally left with no order-placement call.
    return {"executed": False, "real_orders_placed": 0, "refused_reason": "no execution path implemented"}


@router.get("/micro-live/binance-readiness")
async def micro_live_binance_readiness() -> dict:
    """PHASE 2D, item 8 — the Binance-specific micro-live readiness data
    (real account/connectivity/dry-run economics). Deliberately a
    different path from GET /micro-live/readiness (main.py, Reality
    Engine spec sections 59-60): that endpoint is the pre-existing
    general system-health checklist (ledger integrity, capital pool,
    reality capture, stress test, Binance TESTNET ping) with its own
    READY_FOR_CONTROLLED_TEST/NOT_READY verdict — a different question
    from this one. Never returns raw credentials; balances/status come
    from BinanceAccountSnapshot only."""
    settings = get_settings()
    connectivity = await micro_live_orchestrator.check_connectivity()
    snapshot = await micro_live_orchestrator.get_account_snapshot()
    restrictions = await micro_live_orchestrator.get_api_restrictions()
    summary = micro_live_state.summary()
    return {
        "binance_connectivity": connectivity.reachable,
        "credentials_configured": connectivity.credentials_configured,
        "latency_ms": connectivity.latency_ms,
        "connectivity_detail": connectivity.detail,
        "account_mode": "mainnet_read_only",
        "live_trading_enabled": live_guard.live_trading_enabled,
        "real_balance_usdt": snapshot.balance_usdt() if snapshot is not None else None,
        "real_balance_verified": snapshot is not None,
        "account_snapshot_error": micro_live_state.account_snapshot_error,
        "can_trade": snapshot.can_trade if snapshot is not None else None,
        # NOTE: this is the ACCOUNT's overall (KYC/compliance) withdrawal
        # eligibility, not this key's own permission scope — see
        # key_enable_withdrawals below for the field item 2 actually cares about.
        "can_withdraw": snapshot.can_withdraw if snapshot is not None else None,
        "key_enable_reading": restrictions.enable_reading if restrictions is not None else None,
        "key_enable_withdrawals": restrictions.enable_withdrawals if restrictions is not None else None,
        "key_enable_spot_and_margin_trading": restrictions.enable_spot_and_margin_trading if restrictions is not None else None,
        "key_ip_restricted": restrictions.ip_restrict if restrictions is not None else None,
        "api_restrictions_error": micro_live_state.api_restrictions_error,
        "micro_live_cap_usdt": settings.micro_live_cap_usdt,
        "max_live_capital_usdt": settings.max_live_capital_usdt,
        "paper_capital_usd": settings.paper_capital_usd,
        "opportunities_observed": summary["opportunities_observed"],
        "executable_with_cap": summary["executable_with_cap"],
        "non_executable": summary["non_executable"],
        "rejection_reasons": summary["rejection_reasons"],
        "avg_estimated_fees_usd": summary["avg_estimated_fees_usd"],
        "avg_estimated_slippage_pct": summary["avg_estimated_slippage_pct"],
        "avg_net_profit_after_real_constraints_usd": summary["avg_net_profit_after_real_constraints_usd"],
        "live_kill_switch_engaged": live_guard.kill_switch_engaged,
        "real_orders_placed": 0,
    }


def _serialize_distribution(stats: DistributionStats) -> dict:
    return {
        "count": stats.count,
        "mean": stats.mean,
        "median": stats.median,
        "p10": stats.p10,
        "p25": stats.p25,
        "p50": stats.p50,
        "p75": stats.p75,
        "p90": stats.p90,
        "positive_rate_pct": stats.positive_rate_pct,
        "negative_rate_pct": stats.negative_rate_pct,
        "worst": stats.worst,
        "best": stats.best,
        "stdev": stats.stdev,
    }


def _serialize_group(group) -> dict:
    return {
        "key": group.key,
        "observations": group.observations,
        "net_profit": _serialize_distribution(group.net_profit),
        "gross_profit_mean": group.gross_profit_mean,
        "avg_fees_usd": group.avg_fees_usd,
        "avg_slippage_pct": group.avg_slippage_pct,
        "positive_net_rate_pct": group.positive_net_rate_pct,
        "real_fee_coverage_pct": group.real_fee_coverage_pct,
    }


def _serialize_edge_report(report: MicroLiveEdgeReport) -> dict:
    return {
        "observations": report.observations,
        "window_start": report.window_start.isoformat() if report.window_start else None,
        "window_end": report.window_end.isoformat() if report.window_end else None,
        "real_fee_coverage_pct": report.real_fee_coverage_pct,
        "gross_profit": _serialize_distribution(report.gross_profit),
        "net_profit": _serialize_distribution(report.net_profit),
        "net_return_bps": _serialize_distribution(report.net_return_bps),
        "avg_fees_usd": report.avg_fees_usd,
        "avg_slippage_pct": report.avg_slippage_pct,
        "rejection_reasons": report.rejection_reasons,
        "top_positive_symbols": [_serialize_group(g) for g in report.top_positive_symbols()],
        "negative_symbols": [_serialize_group(g) for g in report.negative_symbols()],
        "by_strategy": [_serialize_group(g) for g in report.by_strategy],
        "lunc_usdt": (
            {
                "observations": report.lunc_usdt.observations,
                "net_profit": _serialize_distribution(report.lunc_usdt.net_profit),
                "avg_book_spread_pct": report.lunc_usdt.avg_book_spread_pct,
                "avg_available_depth_usd": report.lunc_usdt.avg_available_depth_usd,
                "avg_slippage_pct": report.lunc_usdt.avg_slippage_pct,
                "min_notional_pass_rate_pct": report.lunc_usdt.min_notional_pass_rate_pct,
                "lot_size_pass_rate_pct": report.lunc_usdt.lot_size_pass_rate_pct,
                "positive_net_rate_pct": report.lunc_usdt.positive_net_rate_pct,
                "real_fee_coverage_pct": report.lunc_usdt.real_fee_coverage_pct,
            }
            if report.lunc_usdt is not None
            else None
        ),
        "time_slices": [
            {
                "slice_start": ts.slice_start.isoformat(),
                "slice_end": ts.slice_end.isoformat(),
                "observations": ts.observations,
                "positive_net_rate_pct": ts.positive_net_rate_pct,
                "mean_net_profit_usd": ts.mean_net_profit_usd,
                "median_net_profit_usd": ts.median_net_profit_usd,
            }
            for ts in report.time_slices
        ],
        "recommended_safety_margin_usd": report.recommended_safety_margin_usd,
        "qualifying_after_gate": report.qualifying_after_gate,
        "real_orders_placed": 0,
    }


@router.get("/micro-live/edge-report")
async def micro_live_edge_report(hours: float = 24.0, session: AsyncSession = Depends(get_session)) -> dict:
    """PHASE 2E — REAL EDGE VALIDATION (user directive, 2026-08-23).
    Aggregates the persisted micro_live_observations table over the last
    `hours` — pure read, changes nothing, places no order."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=hours)
    report = await build_micro_live_edge_report(session, since=since, until=now)
    return _serialize_edge_report(report)


def _serialize_direction(d) -> dict:
    return {
        "direction": d.direction,
        "observations": d.observations,
        "net_profit": _serialize_distribution(d.net_profit),
        "positive_rate_pct": d.positive_rate_pct,
        "real_fee_coverage_pct": d.real_fee_coverage_pct,
    }


def _serialize_dual_leg_report(report: DualLegEdgeReport) -> dict:
    return {
        "observations": report.observations,
        "window_start": report.window_start.isoformat() if report.window_start else None,
        "window_end": report.window_end.isoformat() if report.window_end else None,
        "real_fee_coverage_both_legs_pct": report.real_fee_coverage_both_legs_pct,
        "executable_both_legs_pct": report.executable_both_legs_pct,
        "net_profit": _serialize_distribution(report.net_profit),
        "net_return_bps": _serialize_distribution(report.net_return_bps),
        "dual_leg_latency_ms": _serialize_distribution(report.dual_leg_latency_ms),
        "rejection_reasons": report.rejection_reasons,
        "by_direction": [_serialize_direction(d) for d in report.by_direction],
        "recommended_safety_margin_usd": report.recommended_safety_margin_usd,
        "qualifying_after_gate": report.qualifying_after_gate,
        "qualifying_after_gate_pct": report.qualifying_after_gate_pct,
        "capital_pre_positioning_required": report.capital_pre_positioning_required,
        "real_orders_placed": 0,
    }


@router.get("/dual-leg/edge-report")
async def dual_leg_edge_report(hours: float = 24.0, session: AsyncSession = Depends(get_session)) -> dict:
    """PHASE 2F — DUAL-LEG REALITY VALIDATION (user directive, 2026-08-23).
    Aggregates the persisted dual_leg_observations table — the full
    arbitrage recomputed independently from live data on BOTH legs, never
    opp.expected_profit_usd. Pure read, changes nothing, places no order."""
    from datetime import UTC, datetime, timedelta

    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=hours)
    report = await build_dual_leg_edge_report(session, since=since, until=now)
    return _serialize_dual_leg_report(report)


@router.get("/live/first-gate-report")
async def live_first_gate_report() -> dict:
    """PHASE 3A — FIRST-LIVE GATE (user directive, 2026-08-23). READ-ONLY:
    account permissions, real balances, exchange filters — never places
    an order, never imports anything that could. real_orders_placed is
    always 0 regardless of the verdict."""
    report = await build_first_live_gate_report()
    size = report.smallest_common_order_size
    return {
        "binance_trade_api_ready": report.binance_trade_api_ready,
        "binance_trade_api_detail": report.binance_trade_api_detail,
        "bybit_trade_api_ready": report.bybit_trade_api_ready,
        "bybit_trade_api_detail": report.bybit_trade_api_detail,
        "withdrawals_disabled": report.withdrawals_disabled,
        "withdrawals_detail": report.withdrawals_detail,
        "binance_usdt_balance": report.binance_usdt_balance,
        "bybit_lunc_balance": report.bybit_lunc_balance,
        "max_live_notional_usdt": report.max_live_notional_usdt,
        "leg_risk_protection_pass": report.leg_risk_protection_pass,
        "leg_risk_protection_detail": report.leg_risk_protection_detail,
        "live_kill_switch_pass": report.live_kill_switch_pass,
        "live_kill_switch_detail": report.live_kill_switch_detail,
        "real_pnl_ledger_ready": report.real_pnl_ledger_ready,
        "real_pnl_ledger_detail": report.real_pnl_ledger_detail,
        "smallest_common_order_size": {
            "reachable": size.reachable,
            "reason": size.reason,
            "lunc_qty": size.lunc_qty,
            "notional_usdt": size.notional_usdt,
            "reference_price": size.reference_price,
        },
        "capital_pre_positioned": report.capital_pre_positioned,
        "capital_pre_positioned_detail": report.capital_pre_positioned_detail,
        "ready_for_first_real_arbitrage": report.ready_for_first_real_arbitrage,
        "proposed_first_trade_size_usdt": report.proposed_first_trade_size_usdt,
        "live_trading_enabled": live_guard.live_trading_enabled,
        "real_orders_placed": 0,
    }


@router.get("/market-data/health")
async def market_data_health(symbols: str | None = None) -> list[dict]:
    """Per-(exchange, symbol) quote age for every triangular/stablecoin
    cross-pair (or a custom comma-separated `symbols` list) — diagnostic for
    whether a WS feed has gone quiet (missing entry) or just stale (large
    age_seconds), without needing DB access."""
    now = time.time()
    rows = []
    symbol_list = symbols.split(",") if symbols else TRIANGULAR_CROSS_PAIRS
    for exchange in PRIORITY_EXCHANGES:
        for symbol in symbol_list:
            quote = market_data_store.get_quote(exchange, MarketType.SPOT, symbol)
            rows.append(
                {
                    "exchange": exchange,
                    "symbol": symbol,
                    "present": quote is not None,
                    "age_seconds": round(now - quote.received_at, 2) if quote else None,
                    "bid": quote.bid if quote else None,
                    "ask": quote.ask if quote else None,
                }
            )
    return rows


@router.get("/stress-test/run")
async def stress_test_run(seed: int = 0) -> dict:
    """Reality Engine spec, sections 46-47 — replays a snapshot of the
    market as it looks *right now* through Normal/Stress1/Stress2 latency
    conditions (quote-driven engines only — Stablecoin, Cross-Exchange,
    Triangular) and reports how much of the Normal-condition P&L survives.
    Fully read-only: never touches the live portfolios or position tracker."""
    report = await run_live_stress_test(seed=seed)
    return {
        "net_profit_by_scenario_usd": {scenario.value: profit for scenario, profit in report.net_profit_by_scenario_usd.items()},
        "trades_executed_by_scenario": {scenario.value: result.trades_executed for scenario, result in report.results.items()},
        "opportunities_detected_by_scenario": {
            scenario.value: result.opportunities_detected for scenario, result in report.results.items()
        },
        "robustness_score": report.robustness_score,
    }


@router.get("/opportunities")
async def list_opportunities(limit: int = 50, session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(
        select(OpportunityRecord).order_by(OpportunityRecord.detected_at.desc()).limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "strategy": row.strategy,
            "symbol": row.symbol,
            "gross_spread_pct": row.gross_spread_pct,
            "net_spread_pct": row.net_spread_pct,
            "break_even_pct": row.break_even_pct,
            "execution_mode": row.execution_mode,
            "execution_fill_probability": row.execution_fill_probability,
            "score": row.score,
            "classification": row.classification,
            "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        }
        for row in result.scalars()
    ]
