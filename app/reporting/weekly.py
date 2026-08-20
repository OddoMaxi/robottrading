"""Weekly Analytics + GO/NO-GO verdict (Net Opportunity Engine spec, sections 20-21)."""

import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import extract, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord

# "emergency_unwind" (urgent audit, item 5) is a realized, closed trade —
# see app.reporting.rotation's identical constant for why it belongs here too.
EXECUTED_STATUSES = ("simulated_executed", "partial_fill", "emergency_unwind")

# Starting thresholds — same caveat as the opportunity classification
# thresholds (section 16): recalibrate once real observation data exists.
MIN_SAMPLE_SIZE_FOR_VERDICT = 30
MIN_NET_POSITIVE_RATE_FOR_GO = 0.05  # profitable at least 1 time in 20


class Verdict(StrEnum):
    GO = "go"
    MODIFY = "modify"
    NO_GO = "no_go"


def determine_verdict(sample_size: int, net_positive_rate: float, avg_net_return_pct: float) -> Verdict:
    if sample_size < MIN_SAMPLE_SIZE_FOR_VERDICT:
        return Verdict.MODIFY  # not enough data yet to call it either way
    if net_positive_rate >= MIN_NET_POSITIVE_RATE_FOR_GO and avg_net_return_pct > 0:
        return Verdict.GO
    if net_positive_rate > 0:
        return Verdict.MODIFY
    return Verdict.NO_GO


@dataclass(slots=True)
class StrategyWeeklyStats:
    strategy: str
    total_opportunities: int
    net_profitable: int
    net_positive_rate: float
    avg_net_return_pct: float
    median_net_return_pct: float
    verdict: Verdict


@dataclass(slots=True)
class WeeklyAnalytics:
    period_start: datetime
    period_end: datetime
    total_opportunities: int
    net_profitable_opportunities: int
    executed_simulations: int
    missed_opportunities: int
    net_simulated_pnl_usd: float
    avg_net_return_pct: float
    median_net_return_pct: float
    best_exchange_pair: str | None
    best_asset: str | None
    best_trading_hour_utc: int | None
    by_strategy: list[StrategyWeeklyStats]


def _best_exchange_pair(rows: list[tuple[list | None, float]]) -> str | None:
    pair_returns: dict[str, list[float]] = {}
    for legs, net in rows:
        if not legs or len(legs) < 2:
            continue
        exchanges = sorted({leg.get("exchange") for leg in legs if leg.get("exchange")})
        if len(exchanges) != 2:
            continue
        key = f"{exchanges[0]} / {exchanges[1]}"
        pair_returns.setdefault(key, []).append(float(net))
    if not pair_returns:
        return None
    return max(pair_returns.items(), key=lambda kv: statistics.fmean(kv[1]))[0]


async def build_weekly_analytics(session: AsyncSession, now: datetime | None = None, legs_sample_limit: int = 20_000) -> WeeklyAnalytics:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    period_start = now - timedelta(days=7)
    base_filter = OpportunityRecord.detected_at >= period_start
    has_net = OpportunityRecord.net_spread_pct.isnot(None)

    total_opportunities = (await session.execute(select(func.count()).where(base_filter))).scalar() or 0
    net_profitable = (await session.execute(select(func.count()).where(base_filter, OpportunityRecord.net_spread_pct > 0))).scalar() or 0

    executed_simulations = (
        await session.execute(
            select(func.count()).where(SimulatedTradeRecord.executed_at >= period_start, SimulatedTradeRecord.status.in_(EXECUTED_STATUSES))
        )
    ).scalar() or 0
    missed_opportunities = (
        await session.execute(
            select(func.count()).where(SimulatedTradeRecord.executed_at >= period_start, SimulatedTradeRecord.status == "missed")
        )
    ).scalar() or 0
    net_simulated_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0)).where(SimulatedTradeRecord.executed_at >= period_start)
        )
    ).scalar() or 0.0

    overall_row = (
        await session.execute(
            select(func.avg(OpportunityRecord.net_spread_pct), func.percentile_cont(0.5).within_group(OpportunityRecord.net_spread_pct)).where(
                base_filter, has_net
            )
        )
    ).first()
    avg_net_return_pct = float(overall_row[0]) if overall_row and overall_row[0] is not None else 0.0
    median_net_return_pct = float(overall_row[1]) if overall_row and overall_row[1] is not None else 0.0

    strategy_rows = (
        await session.execute(
            select(
                OpportunityRecord.strategy,
                func.count(),
                func.count().filter(OpportunityRecord.net_spread_pct > 0),
                func.avg(OpportunityRecord.net_spread_pct),
                func.percentile_cont(0.5).within_group(OpportunityRecord.net_spread_pct),
            )
            .where(base_filter, has_net)
            .group_by(OpportunityRecord.strategy)
        )
    ).all()
    by_strategy = []
    for strategy, count, positive_count, avg_return, median_return in strategy_rows:
        rate = positive_count / count if count else 0.0
        avg_return = float(avg_return) if avg_return is not None else 0.0
        by_strategy.append(
            StrategyWeeklyStats(
                strategy=strategy,
                total_opportunities=count,
                net_profitable=positive_count,
                net_positive_rate=rate,
                avg_net_return_pct=avg_return,
                median_net_return_pct=float(median_return) if median_return is not None else 0.0,
                verdict=determine_verdict(count, rate, avg_return),
            )
        )

    best_asset_row = (
        await session.execute(
            select(OpportunityRecord.symbol, func.avg(OpportunityRecord.net_spread_pct))
            .where(base_filter, has_net)
            .group_by(OpportunityRecord.symbol)
            .order_by(func.avg(OpportunityRecord.net_spread_pct).desc())
            .limit(1)
        )
    ).first()
    best_asset = best_asset_row[0] if best_asset_row else None

    best_hour_row = (
        await session.execute(
            select(extract("hour", OpportunityRecord.detected_at), func.avg(OpportunityRecord.net_spread_pct))
            .where(base_filter, has_net)
            .group_by(extract("hour", OpportunityRecord.detected_at))
            .order_by(func.avg(OpportunityRecord.net_spread_pct).desc())
            .limit(1)
        )
    ).first()
    best_trading_hour_utc = int(best_hour_row[0]) if best_hour_row else None

    legs_rows = (
        await session.execute(
            select(OpportunityRecord.legs, OpportunityRecord.net_spread_pct)
            .where(base_filter, has_net)
            .order_by(OpportunityRecord.detected_at.desc())
            .limit(legs_sample_limit)
        )
    ).all()
    best_exchange_pair = _best_exchange_pair(legs_rows)

    return WeeklyAnalytics(
        period_start=period_start,
        period_end=now,
        total_opportunities=total_opportunities,
        net_profitable_opportunities=net_profitable,
        executed_simulations=executed_simulations,
        missed_opportunities=missed_opportunities,
        net_simulated_pnl_usd=float(net_simulated_pnl),
        avg_net_return_pct=avg_net_return_pct,
        median_net_return_pct=median_net_return_pct,
        best_exchange_pair=best_exchange_pair,
        best_asset=best_asset,
        best_trading_hour_utc=best_trading_hour_utc,
        by_strategy=by_strategy,
    )
