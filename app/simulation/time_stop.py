"""Time Stop — 30-minute hard-stop forced exit (FAST TRADING ONLY, user
directive, 2026-08-21).

"30 minutes est uniquement une limite de sécurité, jamais une cible."
app.execution.validator's MAX_NEW_POSITION_HOLDING_SECONDS (20 min) is the
target-side gate that keeps a brand new position from ever approaching
this; this module is the backstop for a position that somehow overstays
it anyway — including the legacy Basis positions found live, opened
before that gate (or Basis/Funding altogether) existed, still holding for
weeks.

Accounting model, chosen deliberately to never invent a positive close:
a forced exit REVERSES the position's originally-credited profit (booked
immediately at open time under this system's "credit now, hold capital
until nominal close" design — it assumed the position would run to its
full, natural completion, which a forced early exit means never happened)
and additionally charges the same realistic unwind cost already used for
a leg-failure emergency unwind (EMERGENCY_UNWIND_COST_PCT of capital).
Net effect is always a small, guaranteed loss — exactly "cette sortie
peut générer une perte, ne jamais inventer une clôture positive".
"""

import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord, SimulatedTradeRecord
from app.reporting.rotation import EXECUTED_STATUSES
from app.simulation.paper_trader import EMERGENCY_UNWIND_COST_PCT, TradeStatus
from app.simulation.portfolios import VirtualPortfolio

logger = logging.getLogger(__name__)

HARD_STOP_HOLDING_SECONDS = 1800.0  # 30 minutes


async def _find_opening_trade(
    session: AsyncSession, portfolio_id: int, strategy: str, exchange: str, symbol: str
) -> tuple[int, object, float, float] | None:
    """The most recent executed trade on this portfolio matching
    (strategy, exchange, symbol) — the one that opened the position
    overdue_locks just flagged. Filtered on (strategy, symbol) in SQL,
    then on the JSON legs[0].exchange in Python (matching the existing
    pattern in app.reporting.simple_summary._reconstruct_open_positions
    rather than a JSON-operator query)."""
    rows = (
        await session.execute(
            select(
                SimulatedTradeRecord.id,
                SimulatedTradeRecord.opportunity_id,
                SimulatedTradeRecord.capital_usd,
                SimulatedTradeRecord.net_profit_usd,
                OpportunityRecord.legs,
            )
            .select_from(SimulatedTradeRecord)
            .join(OpportunityRecord, OpportunityRecord.id == SimulatedTradeRecord.opportunity_id)
            .where(
                SimulatedTradeRecord.portfolio_id == portfolio_id,
                SimulatedTradeRecord.status.in_(EXECUTED_STATUSES),
                OpportunityRecord.strategy == strategy,
                OpportunityRecord.symbol == symbol,
            )
            .order_by(SimulatedTradeRecord.executed_at.desc())
        )
    ).all()
    for trade_id, opportunity_id, capital_usd, net_profit_usd, legs in rows:
        if legs and legs[0].get("exchange") == exchange:
            return trade_id, opportunity_id, float(capital_usd), float(net_profit_usd)
    return None


async def force_exit_overdue_positions(
    session: AsyncSession,
    portfolio: VirtualPortfolio,
    portfolio_id: int,
    now: float,
    max_age_seconds: float = HARD_STOP_HOLDING_SECONDS,
) -> int:
    """Call once per scan (main.py's detection_loop) — cheap when nothing
    is overdue (a single in-memory dict scan, no DB query). Returns how
    many positions were forced out, for logging/monitoring."""
    overdue = portfolio.overdue_locks(now, max_age_seconds)
    for position_key, _locked_amount, age_seconds in overdue:
        strategy, exchange, symbol = position_key.split(":", 2)
        match = await _find_opening_trade(session, portfolio_id, strategy, exchange, symbol)
        if match is None:
            logger.error(
                "TIME STOP: could not find the opening trade for overdue position %s on portfolio %s (age %.0fs) — "
                "releasing the lock without a P&L adjustment, since there's nothing to reverse",
                position_key,
                portfolio.name,
                age_seconds,
            )
            portfolio.force_release_lock(position_key)
            continue

        _trade_id, opportunity_id, capital_usd, original_net_profit_usd = match
        exit_cost_usd = capital_usd * (EMERGENCY_UNWIND_COST_PCT / 100)
        adjustment_usd = -original_net_profit_usd - exit_cost_usd

        portfolio.balances["USDT"] = portfolio.balances.get("USDT", 0.0) + adjustment_usd
        portfolio.force_release_lock(position_key)

        session.add(
            SimulatedTradeRecord(
                opportunity_id=opportunity_id,
                portfolio_id=portfolio_id,
                status=TradeStatus.TIME_STOP_EXIT.value,
                capital_usd=capital_usd,
                gross_profit_usd=0.0,
                fees_usd=-adjustment_usd,
                slippage_usd=0.0,
                net_profit_usd=adjustment_usd,
            )
        )

        logger.warning(
            "TIME STOP EXIT: %s on portfolio %s forced out after %.0fs (limit %.0fs) — "
            "reversed %+.2f original profit, paid %.2f exit cost, net adjustment %+.2f, capital released",
            position_key,
            portfolio.name,
            age_seconds,
            max_age_seconds,
            -original_net_profit_usd,
            exit_cost_usd,
            adjustment_usd,
        )

    return len(overdue)
