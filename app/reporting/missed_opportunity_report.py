"""MISSED PROFITABLE OPPORTUNITIES REPORT (V2.1, user directive,
2026-08-24, item 5) — combines altcoin_scanner.py's persisted
quote-level cause summary (MissedOpportunitySummaryRecord —
INSUFFICIENT_DEPTH, MIN_NOTIONAL, FEES, SAFETY_MARGIN, LATENCY,
EDGE_DISAPPEARED, OTHER) with the two causes only the engine process can
see: INVENTORY_MISSING (reuses app.execution.live_ranker's real balance
check) and CAPITAL_BUSY (reuses app.execution.live_guard's real
in-flight-position count). POSITION_ALREADY_OPEN is reported (currently
always 0, honestly — no real position has ever been opened by this
system, so there is no data source for "the same symbol was already
open" yet).

Read-only, no order. Every figure is THEORETICAL_NOT_REALIZED — no order
was ever placed to earn any of it, and nothing here can place one.
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.database.repository import get_missed_opportunity_summaries
from app.execution.live_guard import live_guard
from app.execution.live_ranker import RankedOpportunity, rank_live_opportunities
from app.scanner.missed_opportunity_tracker import (
    ALL_CAUSES,
    CAUSE_CAPITAL_BUSY,
    CAUSE_INVENTORY_MISSING,
    CAUSE_POSITION_ALREADY_OPEN,
)

DEFAULT_MAX_RANKER_SYMBOLS = 30


@dataclass(slots=True)
class MissedCauseRow:
    cause: str
    count: int
    theoretical_profit_usd_total: float


@dataclass(slots=True)
class MissedOpportunityReport:
    causes: list[MissedCauseRow] = field(default_factory=list)
    total_missed: int = 0
    total_theoretical_profit_usd: float = 0.0
    primary_cause: str | None = None  # the cause with the highest COUNT — "the main reason things get missed", not the costliest single cause


def _inventory_missing_row(ranked: list[RankedOpportunity]) -> MissedCauseRow:
    blocked = [
        r
        for r in ranked
        if r.quote.executable
        and r.quote.net_profit_usd > 0
        and not r.prepositioning.executable_now
        and r.prepositioning.available_sell_balance < r.prepositioning.required_sell_qty
    ]
    return MissedCauseRow(CAUSE_INVENTORY_MISSING, len(blocked), sum(r.quote.net_profit_usd for r in blocked))


def _capital_busy_row(ranked: list[RankedOpportunity], in_flight_count: int, max_concurrent: int) -> MissedCauseRow:
    if in_flight_count < max_concurrent:
        return MissedCauseRow(CAUSE_CAPITAL_BUSY, 0, 0.0)
    # at capacity: every OTHER currently-qualified, pre-positioned
    # opportunity is genuinely capital-busy, not just theoretically missed
    qualified = [r for r in ranked if r.quote.executable and r.quote.net_profit_usd > 0 and r.prepositioning.executable_now]
    return MissedCauseRow(CAUSE_CAPITAL_BUSY, len(qualified), sum(r.quote.net_profit_usd for r in qualified))


async def build_missed_opportunity_report(session: AsyncSession, max_ranker_symbols: int = DEFAULT_MAX_RANKER_SYMBOLS) -> MissedOpportunityReport:
    settings = get_settings()

    persisted = await get_missed_opportunity_summaries(session)
    causes: dict[str, MissedCauseRow] = {
        row.cause: MissedCauseRow(cause=row.cause, count=row.count, theoretical_profit_usd_total=float(row.theoretical_profit_usd_total))
        for row in persisted
    }

    ranked: list[RankedOpportunity] = []
    try:
        ranked = await rank_live_opportunities(max_symbols=max_ranker_symbols)
    except Exception:
        ranked = []

    causes[CAUSE_INVENTORY_MISSING] = _inventory_missing_row(ranked)
    causes[CAUSE_CAPITAL_BUSY] = _capital_busy_row(ranked, live_guard.in_flight_count, settings.max_concurrent_live_arbitrages)
    causes.setdefault(CAUSE_POSITION_ALREADY_OPEN, MissedCauseRow(CAUSE_POSITION_ALREADY_OPEN, 0, 0.0))

    for cause in ALL_CAUSES:
        causes.setdefault(cause, MissedCauseRow(cause, 0, 0.0))

    rows = sorted(causes.values(), key=lambda r: r.theoretical_profit_usd_total, reverse=True)
    total_missed = sum(r.count for r in rows)
    total_profit = sum(r.theoretical_profit_usd_total for r in rows)
    primary = max(rows, key=lambda r: r.count).cause if total_missed > 0 else None

    return MissedOpportunityReport(
        causes=rows, total_missed=total_missed, total_theoretical_profit_usd=round(total_profit, 6), primary_cause=primary
    )
