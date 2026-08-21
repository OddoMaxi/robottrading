"""Portfolio State Recovery (Continuous Execution spec — urgent audit fix).

VirtualPortfolio.balances (its actual $ equity) and ._locked (open-position
capital reservations), plus OpenPositionTracker, are all in-memory only. A
process restart — a deploy, a crash, an OOM kill — silently forgets both:

  - every currently-open position, so the engine re-opens a *fresh*
    position on top of one that, per the trade ledger, is still locked for
    weeks (Basis/Funding hold for up to ~35 days);
  - all historical profit, since a freshly-constructed VirtualPortfolio
    starts at exactly initial_capital_usd — the engine's own capital
    sizing (compound mode references current balance) would silently
    revert to under-sizing every trade after a restart.

Confirmed in production: every engine restart on 2026-08-20 re-opened a
new ~$1,000 BTC/ETH basis position within 1-3 seconds of startup, on top
of 14+ already open and unexpired ones. This rebuilds both from the trade
ledger before the detection loop starts.

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

from sqlalchemy import func, select
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


async def rebuild_portfolio_state(
    session: AsyncSession,
    portfolios: list[VirtualPortfolio],
    portfolio_ids: dict[str, int],
    position_tracker: OpenPositionTracker,
    now: float | None = None,
) -> int:
    """Returns how many open positions were recovered, for a startup log line."""
    now = now if now is not None else time.time()
    now_dt = datetime.fromtimestamp(now, tz=UTC).replace(tzinfo=None)
    cutoff = now_dt - timedelta(days=RECOVERY_LOOKBACK_DAYS)

    total_recovered = 0
    for portfolio in portfolios:
        portfolio_id = portfolio_ids[portfolio.name]

        # 1. Balance — a freshly-constructed VirtualPortfolio starts at
        # exactly initial_capital_usd with none of its historical profit.
        # Reconstruct true accumulated equity from the full, all-time
        # ledger (same semantics as app.reporting.simple_summary.build_portfolio_capital,
        # which the dashboard already uses for the same reason).
        lifetime_pnl = (
            await session.execute(
                select(func.coalesce(func.sum(SimulatedTradeRecord.net_profit_usd), 0)).where(
                    SimulatedTradeRecord.portfolio_id == portfolio_id,
                    SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                )
            )
        ).scalar() or 0.0
        portfolio.balances["USDT"] = portfolio.initial_capital_usd + float(lifetime_pnl)

        # 2. Open positions / capital locks.
        rows = (
            await session.execute(
                select(
                    SimulatedTradeRecord.capital_usd,
                    SimulatedTradeRecord.net_profit_usd,
                    SimulatedTradeRecord.executed_at,
                    SimulatedTradeRecord.status,
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

        # FAST TRADING ONLY bug found live, 2026-08-21 — a time_stop_exit
        # record is a settling event, not an opening one; without this map,
        # a restart would reconstruct the position from its ORIGINAL
        # opening trade (whose own nominal holding_period_seconds is
        # unaware time_stop already closed it), re-lock it, and the very
        # next scan's time_stop check would find app.simulation.time_stop's
        # own PREVIOUS adjustment record as "the opening trade" and reverse
        # ITS reversal — flipping the sign positive. See
        # app.reporting.simple_summary's identical fix for the dashboard
        # reconstruction side of this same bug class.
        force_closed_at: dict[str, datetime] = {}
        for _capital_usd, _net_profit_usd, executed_at, status, strategy, symbol, legs, _holding_period_seconds in rows:
            if status == "time_stop_exit":
                exchange = legs[0].get("exchange") if legs else None
                key = f"{strategy}:{exchange}:{symbol}"
                if key not in force_closed_at or executed_at > force_closed_at[key]:
                    force_closed_at[key] = executed_at

        # Earliest-wins per key — see module docstring for why later
        # overlapping trades on the same key are treated as bug artifacts,
        # not additional independent positions.
        earliest_open_by_key: dict[str, tuple] = {}
        for capital_usd, net_profit_usd, executed_at, status, strategy, symbol, legs, holding_period_seconds in rows:
            if status == "time_stop_exit":
                continue  # a settling event, never itself a position to reopen
            exchange = legs[0].get("exchange") if legs else None
            key = f"{strategy}:{exchange}:{symbol}"
            if key in force_closed_at:
                continue  # already closed by a later time_stop_exit — nothing to reconstruct
            closes_at = executed_at + timedelta(seconds=float(holding_period_seconds))
            if closes_at <= now_dt or key in earliest_open_by_key:
                continue
            earliest_open_by_key[key] = (capital_usd, net_profit_usd, executed_at, closes_at, strategy, exchange, symbol)

        for key, (capital_usd, net_profit_usd, executed_at, closes_at, strategy, exchange, symbol) in earliest_open_by_key.items():
            remaining_seconds = (closes_at - now_dt).total_seconds()
            if remaining_seconds <= 0:
                continue
            # Block re-entry on this key regardless of whether the capital
            # reservation below succeeds — it's still a real, still-open
            # historical position; we just may not be able to safely
            # account for its capital under today's risk limits (see below).
            position_tracker.open_position((strategy, exchange, symbol), now, remaining_seconds)
            # opened_at must be the position's TRUE original executed_at,
            # not this restart's `now` (FAST TRADING ONLY bug found live,
            # 2026-08-21) — otherwise every restart resets every open
            # position's age to zero, permanently defeating time_stop's
            # 30-minute hard stop for anything that survives a restart.
            opened_at_epoch = executed_at.replace(tzinfo=UTC).timestamp()
            reserved = portfolio.lock_capital(
                key, float(capital_usd) + float(net_profit_usd), now + remaining_seconds, now=now, opened_at=opened_at_epoch
            )
            if not reserved:
                # A position sized under since-superseded rules (e.g.
                # pre-dating the % risk-limit fix) can be larger than
                # today's invariant allows to reserve. Never violate
                # available_capital >= 0 to force it in — log it and move
                # on; its capital effectively free-floats until it closes.
                logger.error(
                    "state recovery: could not fully reserve stale position %s (%.2f) on portfolio %s without violating "
                    "available_capital >= 0 — likely sized under old risk rules; left unreserved, not double-counted",
                    key,
                    float(capital_usd) + float(net_profit_usd),
                    portfolio.name,
                )
                continue
            total_recovered += 1

    logger.warning(
        "state recovery: rebuilt balances for %d portfolio(s), %d open position(s), from the trade ledger after restart",
        len(portfolios),
        total_recovered,
    )
    return total_recovered
