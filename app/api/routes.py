import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import PRIORITY_EXCHANGES, TRIANGULAR_CROSS_PAIRS, MarketType
from app.database.models import OpportunityRecord
from app.database.session import get_session
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
async def master_rollback(body: MasterRollbackRequest) -> dict:
    """Instant rollback to OLD as sole paper authority — a pure in-memory
    flag flip (app.orchestration.control.master_control), no data
    reconstruction, takes effect on the very next scan cycle for every
    cutover-gated call site in main.py."""
    master_control.disable(body.reason)
    return {"paper_authority_enabled": False, "reason": body.reason}


@router.post("/master/enable")
async def master_enable() -> dict:
    master_control.enable()
    return {"paper_authority_enabled": True}


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
