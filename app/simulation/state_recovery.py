"""Position/Capital State Recovery (Continuous Execution spec — urgent audit fix).

VirtualPortfolio._locked and OpenPositionTracker are in-memory only. A
process restart — a deploy, a crash, an OOM kill — silently forgets every
currently-open position, and the engine then happily "opens" a fresh
position on top of one that, per the trade ledger, is still locked for
weeks (Basis/Funding hold for up to ~35 days). Confirmed in production:
every engine restart on 2026-08-20 re-opened a new ~$1,000 BTC/ETH basis
position within 1-3 seconds of startup, on top of 14+ already open and
unexpired ones — because the tracker had no memory of them at all. This
rebuilds both from the trade ledger before the detection loop starts.

For a given (portfolio, strategy, exchange, symbol) key, only the
*earliest* still-open trade is treated as the real, legitimate position.
Every later trade whose window overlapped an earlier one on the same key
is itself a symptom of this exact bug — a duplicate opened while the real
position should still have been blocking re-entry — not a second,
independent position, so it is not re-locked.
"""

import logging
import time
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES
from app.simulation.portfolios import VirtualPortfolio
from app.simulation.position_tracker import OpenPositionTracker

logger = logging.getLogger(__name__)

# Basis/Funding hold up to ~90 days (quarterly futures worst case) — wide
# enough to catch every currently-open position, bounded so this stays a
# fast, indexed portfolio_id scan rather than reading the whole ledger.
RECOVERY_LOOKBACK_DAYS = 120


async def rebuild_open_positions(
    session: AsyncSession,
    portfolios: list[VirtualPortfolio],
    portfolio_ids: dict[str, int],
    position_tracker: OpenPositionTracker,
    now: float | None = None,
) -> int:
    """Returns how many positions were recovered, for a startup log line."""
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=UTC).replace(tzinfo=None)
    cutoff = now_dt - timedelta(days=RECOVERY_LOOKBACK_DAYS)

    total_recovered = 0
    for portfolio in portfolios:
        portfolio_id = portfolio_ids[portfolio.name]
        rows = (
            await session.execute(
                select(
                    SimulatedTradeRecord.capital_usd,
                    SimulatedTradeRecord.net_profit_usd,
                    SimulatedTradeRecord.executed_at,
                    OpportunityRecord.strategy,
                    OpportunityRecord.symbol,
                    OpportunityRecord.legs,
                    OpportunityRecord.holding_period_seconds,
                )
                .select_from(SimulatedTradeRecord)
                .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
                .where(
                    SimulatedTradeRecord.portfolio_id == portfolio_id,
                    SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                    OpportunityRecord.holding_period_seconds.is_not(None),
                    SimulatedTradeRecord.executed_at >= cutoff,
                )
                .order_by(SimulatedTradeRecord.executed_at.asc())
            )
        ).all()

        # Earliest-wins per key — see module docstring for why later
        # overlapping trades on the same key are treated as bug artifacts,
        # not additional independent positions.
        earliest_open_by_key: dict[str, tuple] = {}
        for capital_usd, net_profit_usd, executed_at, strategy, symbol, legs, holding_period_seconds in rows:
            exchange = legs[0].get("exchange") if legs else None
            key = f"{strategy}:{exchange}:{symbol}"
            closes_at = executed_at + timedelta(seconds=float(holding_period_seconds))
            if closes_at <= now_dt or key in earliest_open_by_key:
                continue
            earliest_open_by_key[key] = (capital_usd, net_profit_usd, closes_at, strategy, exchange, symbol)

        for key, (capital_usd, net_profit_usd, closes_at, strategy, exchange, symbol) in earliest_open_by_key.items():
            remaining_seconds = (closes_at - now_dt).total_seconds()
            if remaining_seconds <= 0:
                continue
            portfolio.lock_capital(key, float(capital_usd) + float(net_profit_usd), now + remaining_seconds)
            position_tracker.open_position((strategy, exchange, symbol), now, remaining_seconds)
            total_recovered += 1

    if total_recovered:
        logger.warning("state recovery: rebuilt %d open position(s) from the trade ledger after restart", total_recovered)
    return total_recovered
