"""REAL TRADING — dashboard summary (Phase 3, item 10, user directive,
2026-08-23) — READ-ONLY. Aggregates the Profit Reality Ledger
(live_arbitrage_executions) into exactly the fields the dashboard's new
"REAL TRADING" section needs. Never computes anything from the paper
engine's tables — paper and real stay strictly separate sources.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LiveArbitrageExecutionRecord


@dataclass(slots=True)
class LastRealTrade:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    outcome: str
    actual_net_pnl_usd: float | None
    started_at: datetime


@dataclass(slots=True)
class RealTradingSummary:
    total_real_trades: int  # BOTH_FILLED only
    wins: int
    losses: int
    win_rate_pct: float | None
    today_real_pnl_usd: float
    total_real_pnl_usd: float
    total_real_fees_usd: float
    average_profit_per_trade_usd: float | None
    last_trades: list[LastRealTrade] = field(default_factory=list)


async def build_real_trading_summary(session: AsyncSession, now: datetime | None = None, last_n: int = 10) -> RealTradingSummary:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    stmt = select(LiveArbitrageExecutionRecord).where(LiveArbitrageExecutionRecord.outcome == "both_filled").order_by(LiveArbitrageExecutionRecord.started_at)
    rows = list((await session.execute(stmt)).scalars().all())

    return compute_real_trading_summary(rows, now=now, today_start=today_start, last_n=last_n)


def compute_real_trading_summary(rows: list[LiveArbitrageExecutionRecord], now: datetime, today_start: datetime, last_n: int = 10) -> RealTradingSummary:
    """Pure computation, split out for unit testing without a real DB."""
    pnls = [float(r.actual_net_pnl_usd) for r in rows if r.actual_net_pnl_usd is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p <= 0)
    win_rate = wins / len(pnls) * 100 if pnls else None

    today_rows = [r for r in rows if r.started_at is not None and r.started_at >= today_start]
    today_pnl = sum(float(r.actual_net_pnl_usd) for r in today_rows if r.actual_net_pnl_usd is not None)

    total_fees = sum(
        (float(r.buy_fees_usd) if r.buy_fees_usd is not None else 0.0) + (float(r.sell_fees_usd) if r.sell_fees_usd is not None else 0.0)
        for r in rows
    )
    avg_profit = statistics.fmean(pnls) if pnls else None

    last_trades = [
        LastRealTrade(
            symbol=r.symbol, buy_exchange=r.buy_exchange, sell_exchange=r.sell_exchange, outcome=r.outcome,
            actual_net_pnl_usd=float(r.actual_net_pnl_usd) if r.actual_net_pnl_usd is not None else None, started_at=r.started_at,
        )
        for r in sorted(rows, key=lambda r: r.started_at, reverse=True)[:last_n]
    ]

    return RealTradingSummary(
        total_real_trades=len(rows),
        wins=wins,
        losses=losses,
        win_rate_pct=win_rate,
        today_real_pnl_usd=today_pnl,
        total_real_pnl_usd=sum(pnls),
        total_real_fees_usd=total_fees,
        average_profit_per_trade_usd=avg_profit,
        last_trades=last_trades,
    )
