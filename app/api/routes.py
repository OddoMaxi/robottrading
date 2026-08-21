import time

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import PRIORITY_EXCHANGES, TRIANGULAR_CROSS_PAIRS, MarketType
from app.database.models import OpportunityRecord
from app.database.session import get_session
from app.market_data.store import market_data_store
from app.risk.risk_engine import risk_engine

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
