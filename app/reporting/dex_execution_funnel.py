"""DEX Execution Funnel (Multi-Market Opportunity Engine, V5.5 continuation,
user directive, 2026-08-22).

The complete per-strategy funnel: Detected -> Gross positive -> Net
positive after ALL costs -> Executable -> Attempted -> Filled -> Failed ->
Edge disappeared -> Profitable -> Losing, with counts AND conversion rates
between stages, exactly as requested. Reads app.onchain.dex_paper_trader's
own dex_simulated_trades table (never simulated_trades — see that model's
docstring) joined back to opportunities for the pre-attempt stages.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DexSimulatedTradeRecord, OpportunityRecord

DEX_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic", "flash_loan_research")
DEX_ATTEMPTABLE_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic")


@dataclass(slots=True)
class DexStrategyFunnel:
    strategy: str
    detected: int
    gross_positive: int
    net_positive: int
    executable: int
    attempts: int
    filled: int
    edge_disappeared: int
    failed: int
    no_capital_available: int
    profitable: int
    losing: int
    capital_used_usd: float
    avg_capital_lock_ms: float | None
    avg_net_profit_usd: float | None
    total_net_profit_usd: float
    net_profit_per_capital_minute_usd: float | None

    def conversion_pct(self, numerator: int, denominator: int) -> float | None:
        return round(numerator / denominator * 100, 1) if denominator else None


def _build_funnel(strategy: str, detection_row, attempt_row) -> DexStrategyFunnel:
    detected, gross_positive, net_positive, executable = (int(x or 0) for x in detection_row)
    attempts, filled, edge_disappeared, failed, no_capital, profitable, losing, capital_used, total_net_profit, avg_lock_ms = attempt_row

    attempts = int(attempts or 0)
    filled = int(filled or 0)
    capital_used = float(capital_used or 0.0)
    total_net_profit = float(total_net_profit or 0.0)

    return DexStrategyFunnel(
        strategy=strategy,
        detected=detected,
        gross_positive=gross_positive,
        net_positive=net_positive,
        executable=executable,
        attempts=attempts,
        filled=filled,
        edge_disappeared=int(edge_disappeared or 0),
        failed=int(failed or 0),
        no_capital_available=int(no_capital or 0),
        profitable=int(profitable or 0),
        losing=int(losing or 0),
        capital_used_usd=capital_used,
        avg_capital_lock_ms=float(avg_lock_ms) if avg_lock_ms is not None else None,
        avg_net_profit_usd=(total_net_profit / attempts) if attempts else None,
        total_net_profit_usd=total_net_profit,
        net_profit_per_capital_minute_usd=(
            total_net_profit / (capital_used * (float(avg_lock_ms or 0.0) / 60_000.0)) if capital_used > 0 and avg_lock_ms else None
        ),
    )


async def build_dex_execution_funnel(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> list[DexStrategyFunnel]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    gross_positive_case = case((OpportunityRecord.gross_spread_pct > 0, 1), else_=0)
    net_positive_case = case((OpportunityRecord.net_spread_pct > 0, 1), else_=0)
    executable_case = case((OpportunityRecord.rejection_reason.is_(None), 1), else_=0)
    detection_rows = (
        await session.execute(
            select(
                OpportunityRecord.strategy,
                func.count(),
                func.coalesce(func.sum(gross_positive_case), 0),
                func.coalesce(func.sum(net_positive_case), 0),
                func.coalesce(func.sum(executable_case), 0),
            )
            .where(OpportunityRecord.detected_at >= cutoff, OpportunityRecord.strategy.in_(DEX_STRATEGIES))
            .group_by(OpportunityRecord.strategy)
        )
    ).all()
    detection_by_strategy = {row[0]: row[1:] for row in detection_rows}

    filled_case = case((DexSimulatedTradeRecord.status == "dex_filled", 1), else_=0)
    edge_disappeared_case = case((DexSimulatedTradeRecord.status == "dex_edge_disappeared", 1), else_=0)
    failed_case = case((DexSimulatedTradeRecord.status == "dex_failed", 1), else_=0)
    no_capital_case = case((DexSimulatedTradeRecord.status == "dex_no_capital_available", 1), else_=0)
    profitable_case = case((DexSimulatedTradeRecord.net_profit_usd > 0, 1), else_=0)
    losing_case = case((DexSimulatedTradeRecord.net_profit_usd < 0, 1), else_=0)
    attempt_rows = (
        await session.execute(
            select(
                DexSimulatedTradeRecord.strategy,
                func.count(),
                func.coalesce(func.sum(filled_case), 0),
                func.coalesce(func.sum(edge_disappeared_case), 0),
                func.coalesce(func.sum(failed_case), 0),
                func.coalesce(func.sum(no_capital_case), 0),
                func.coalesce(func.sum(profitable_case), 0),
                func.coalesce(func.sum(losing_case), 0),
                func.coalesce(func.sum(DexSimulatedTradeRecord.capital_usd), 0),
                func.coalesce(func.sum(DexSimulatedTradeRecord.net_profit_usd), 0),
                func.avg(DexSimulatedTradeRecord.validation_to_execution_ms),
            )
            .where(DexSimulatedTradeRecord.execution_complete_at >= cutoff, DexSimulatedTradeRecord.strategy.in_(DEX_STRATEGIES))
            .group_by(DexSimulatedTradeRecord.strategy)
        )
    ).all()
    attempt_by_strategy = {row[0]: row[1:] for row in attempt_rows}

    all_strategies = sorted(set(detection_by_strategy) | set(attempt_by_strategy))
    zero_detection = (0, 0, 0, 0)
    zero_attempt = (0, 0, 0, 0, 0, 0, 0, 0.0, 0.0, None)
    return [
        _build_funnel(strategy, detection_by_strategy.get(strategy, zero_detection), attempt_by_strategy.get(strategy, zero_attempt))
        for strategy in all_strategies
    ]
