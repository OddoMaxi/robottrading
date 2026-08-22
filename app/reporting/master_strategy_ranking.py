"""Master Strategy Ranking + Capital Velocity + Trades/Hour + Capture Rate
(V5/V5.5 Master Orchestration, user directive, 2026-08-22, spec Parts
V/W/X/Y).

One combined, directly-comparable view across BOTH engines' strategies —
CEX's ledger (simulated_trades) and DEX's ledger (dex_simulated_trades)
are queried with the same shape (attempted/filled/capital_used/net_profit/
avg_duration) so "CEX Cross Exchange" and "DEX Atomic" land in one
sortable table, per spec Part V's explicit ask, rather than stitching
together two differently-shaped per-engine reports at display time.

capital_velocity_usd_per_capital_minute = net_profit / (capital_used *
avg_duration_minutes) — the same "profit per dollar per minute" measure
app.opportunity.master_ranker already scores individual opportunities
with, aggregated here per strategy.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DexSimulatedTradeRecord, OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES

DEX_FILLED_STATUS = "dex_filled"


@dataclass(slots=True)
class StrategyPerformance:
    engine: str  # "CEX" or "DEX"
    strategy: str
    attempts: int
    filled: int
    net_profit_usd: float
    capital_used_usd: float
    avg_duration_seconds: float | None
    attempts_per_hour: float
    filled_per_hour: float
    profitable_per_hour: float
    capture_rate_pct: float | None  # filled / attempts
    capital_velocity_usd_per_minute: float | None  # net_profit / (capital_used_usd * avg_duration_minutes)


def _capital_velocity(net_profit_usd: float, capital_used_usd: float, avg_duration_seconds: float | None) -> float | None:
    if capital_used_usd <= 0 or not avg_duration_seconds or avg_duration_seconds <= 0:
        return None
    avg_duration_minutes = avg_duration_seconds / 60.0
    return net_profit_usd / (capital_used_usd * avg_duration_minutes)


async def _cex_strategy_rows(session: AsyncSession, cutoff: datetime, hours: float) -> list[StrategyPerformance]:
    filled_case = case((SimulatedTradeRecord.status.in_(EXECUTED_STATUSES), 1), else_=0)
    profitable_case = case((SimulatedTradeRecord.net_profit_usd > 0, 1), else_=0)
    rows = (
        await session.execute(
            select(
                OpportunityRecord.strategy,
                func.count(),
                func.coalesce(func.sum(filled_case), 0),
                func.coalesce(func.sum(profitable_case), 0),
                func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0),
                func.coalesce(func.sum(SimulatedTradeRecord.capital_usd), 0),
                func.avg(OpportunityRecord.holding_period_seconds),
            )
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(SimulatedTradeRecord.executed_at >= cutoff)
            .group_by(OpportunityRecord.strategy)
        )
    ).all()

    out = []
    for strategy, attempts, filled, profitable, net_profit, capital_used, avg_duration in rows:
        attempts, filled, profitable = int(attempts), int(filled), int(profitable)
        net_profit, capital_used = float(net_profit or 0.0), float(capital_used or 0.0)
        avg_duration = float(avg_duration) if avg_duration is not None else None
        out.append(
            StrategyPerformance(
                engine="CEX",
                strategy=strategy,
                attempts=attempts,
                filled=filled,
                net_profit_usd=net_profit,
                capital_used_usd=capital_used,
                avg_duration_seconds=avg_duration,
                attempts_per_hour=(attempts / hours) if hours else 0.0,
                filled_per_hour=(filled / hours) if hours else 0.0,
                profitable_per_hour=(profitable / hours) if hours else 0.0,
                capture_rate_pct=(filled / attempts * 100) if attempts else None,
                capital_velocity_usd_per_minute=_capital_velocity(net_profit, capital_used, avg_duration),
            )
        )
    return out


async def _dex_strategy_rows(session: AsyncSession, cutoff: datetime, hours: float) -> list[StrategyPerformance]:
    filled_case = case((DexSimulatedTradeRecord.status == DEX_FILLED_STATUS, 1), else_=0)
    profitable_case = case((DexSimulatedTradeRecord.net_profit_usd > 0, 1), else_=0)
    rows = (
        await session.execute(
            select(
                DexSimulatedTradeRecord.strategy,
                func.count(),
                func.coalesce(func.sum(filled_case), 0),
                func.coalesce(func.sum(profitable_case), 0),
                func.coalesce(func.sum(DexSimulatedTradeRecord.net_profit_usd), 0),
                func.coalesce(func.sum(DexSimulatedTradeRecord.capital_usd), 0),
                func.avg(DexSimulatedTradeRecord.validation_to_execution_ms),
            )
            .where(DexSimulatedTradeRecord.execution_complete_at >= cutoff)
            .group_by(DexSimulatedTradeRecord.strategy)
        )
    ).all()

    out = []
    for strategy, attempts, filled, profitable, net_profit, capital_used, avg_duration_ms in rows:
        attempts, filled, profitable = int(attempts), int(filled), int(profitable)
        net_profit, capital_used = float(net_profit or 0.0), float(capital_used or 0.0)
        avg_duration = (float(avg_duration_ms) / 1000.0) if avg_duration_ms is not None else None
        out.append(
            StrategyPerformance(
                engine="DEX",
                strategy=strategy,
                attempts=attempts,
                filled=filled,
                net_profit_usd=net_profit,
                capital_used_usd=capital_used,
                avg_duration_seconds=avg_duration,
                attempts_per_hour=(attempts / hours) if hours else 0.0,
                filled_per_hour=(filled / hours) if hours else 0.0,
                profitable_per_hour=(profitable / hours) if hours else 0.0,
                capture_rate_pct=(filled / attempts * 100) if attempts else None,
                capital_velocity_usd_per_minute=_capital_velocity(net_profit, capital_used, avg_duration),
            )
        )
    return out


async def build_master_strategy_ranking(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> list[StrategyPerformance]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)
    cex_rows = await _cex_strategy_rows(session, cutoff, hours)
    dex_rows = await _dex_strategy_rows(session, cutoff, hours)
    return sorted(cex_rows + dex_rows, key=lambda r: r.net_profit_usd, reverse=True)
