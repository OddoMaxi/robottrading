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
from app.execution.micro_live import micro_live_orchestrator, micro_live_state
from app.market_data.store import market_data_store
from app.orchestration.control import master_control
from app.orchestration.global_allocator import global_allocator
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
        "can_withdraw": snapshot.can_withdraw if snapshot is not None else None,
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
