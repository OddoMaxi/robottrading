"""Capital-Tier Constrained Replay (V5.5 REALITY AUDIT, user directive,
2026-08-22, section 7).

"Un moteur reel de reservation/concurrence de capital... teste a
$1K/$2.5K/$5K/$10K/$25K. Interdiction explicite de supposer que $5,000
peuvent financer plusieurs trades simultanes de $5,000." This module
replays a chronologically-ordered list of historical, ALREADY-DEDUPLICATED
opportunities (one execution method per real economic event — see
main.py's duplicate_economic_event fix) through the REAL, fixed
app.onchain.dex_paper_trader.attempt_dex_trade / DexCapitalPool pipeline —
not a separate simulation — against a fresh pool at each tier size, so
genuine capital contention (the reality audit's section 7 finding) is
exercised exactly as production now behaves, at 5 different budget sizes.

Historical per-opportunity gas_cost_usd is NOT persisted (only the
already-net realistic_executable_edge_pct is) — callers must supply a
gas_cost_usd_by_chain map rather than this module fabricating one
silently.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.constants import Strategy
from app.database.models import OpportunityRecord
from app.onchain.dex_paper_trader import DEX_ATTEMPTABLE_STRATEGIES, DexCapitalPool, DexTradeStatus, attempt_dex_trade
from app.opportunity.models import Opportunity

CAPITAL_TIERS_USD = [1_000.0, 2_500.0, 5_000.0, 10_000.0, 25_000.0]


@dataclass(slots=True)
class CapitalTierReplayResult:
    capital_tier_usd: float
    n_opportunities: int
    n_filled: int
    n_no_capital_available: int
    n_edge_disappeared: int
    n_not_profitable_at_size: int
    n_failed: int
    total_net_profit_usd: float
    max_simultaneous_locked_usd: float
    avg_utilization_pct: float | None  # mean(locked / tier) sampled at each attempt


def replay_at_capital_tier(
    opportunities: list[Opportunity],
    capital_tier_usd: float,
    gas_cost_usd_by_chain: dict[str, float],
    default_gas_cost_usd: float,
    rng,
) -> CapitalTierReplayResult:
    pool = DexCapitalPool(total_capital_usd=capital_tier_usd)
    ordered = sorted(opportunities, key=lambda o: o.detected_at or 0.0)

    counts = {status: 0 for status in DexTradeStatus}
    total_net_profit_usd = 0.0
    max_locked = 0.0
    utilizations: list[float] = []

    for opp in ordered:
        now = opp.detected_at or 0.0
        chain = opp.legs[0].get("chain") if opp.legs else None
        gas_cost_usd = gas_cost_usd_by_chain.get(chain, default_gas_cost_usd)
        attempt = attempt_dex_trade(opp, pool, gas_cost_usd, rng, now=now)
        counts[attempt.status] += 1
        total_net_profit_usd += attempt.net_profit_usd

        locked_now = pool.locked_capital_usd(now)
        max_locked = max(max_locked, locked_now)
        utilizations.append(locked_now / capital_tier_usd * 100)

    return CapitalTierReplayResult(
        capital_tier_usd=capital_tier_usd,
        n_opportunities=len(ordered),
        n_filled=counts[DexTradeStatus.FILLED],
        n_no_capital_available=counts[DexTradeStatus.NO_CAPITAL_AVAILABLE],
        n_edge_disappeared=counts[DexTradeStatus.EDGE_DISAPPEARED],
        n_not_profitable_at_size=counts[DexTradeStatus.NOT_PROFITABLE_AT_SIZE],
        n_failed=counts[DexTradeStatus.FAILED],
        total_net_profit_usd=total_net_profit_usd,
        max_simultaneous_locked_usd=max_locked,
        avg_utilization_pct=(sum(utilizations) / len(utilizations)) if utilizations else None,
    )


def replay_across_tiers(
    opportunities: list[Opportunity],
    gas_cost_usd_by_chain: dict[str, float],
    default_gas_cost_usd: float,
    rng_factory,
    tiers_usd: list[float] = CAPITAL_TIERS_USD,
) -> list[CapitalTierReplayResult]:
    """rng_factory() -> a fresh rng per tier, so every tier replays against
    the SAME random draws (fill-probability rolls, revalidation drift) —
    isolating capital-tier effects from run-to-run randomness."""
    return [replay_at_capital_tier(opportunities, tier, gas_cost_usd_by_chain, default_gas_cost_usd, rng_factory()) for tier in tiers_usd]


_STRATEGY_BY_VALUE = {s.value: s for s in Strategy}


async def fetch_deduplicated_opportunities_since(session: AsyncSession, since: datetime) -> list[Opportunity]:
    """Live-data source for the dashboard's Capital-Tier Replay panel
    (spec Part AC) — never hardcode a past manual run's numbers. Since
    main.py's duplicate_economic_event fix (app.reporting.reality_baseline.
    REALITY_BASELINE_AT) now marks the lower-EV twin of every duplicate
    pair live at detection time, deduplication here is just excluding that
    rejection_reason — no self-join reconstruction needed, unlike the
    audit's original one-off historical analysis of PRE-fix data."""
    rows = (
        await session.execute(
            select(
                OpportunityRecord.id, OpportunityRecord.strategy, OpportunityRecord.symbol, OpportunityRecord.legs,
                OpportunityRecord.capital_usd, OpportunityRecord.realistic_executable_edge_pct,
                OpportunityRecord.execution_fill_probability, OpportunityRecord.detected_at,
            ).where(
                OpportunityRecord.strategy.in_(DEX_ATTEMPTABLE_STRATEGIES),
                OpportunityRecord.detected_at >= since,
                OpportunityRecord.capital_usd.is_not(None),
                OpportunityRecord.realistic_executable_edge_pct.is_not(None),
                OpportunityRecord.rejection_reason.is_distinct_from("duplicate_economic_event"),
            )
        )
    ).all()
    opportunities = []
    for id_, strategy, symbol, legs, capital_usd, edge_pct, fill_prob, detected_at in rows:
        opportunities.append(
            Opportunity(
                strategy=_STRATEGY_BY_VALUE.get(strategy, strategy),
                symbol=symbol,
                legs=legs or [],
                gross_spread_pct=0.0,
                capital_usd=float(capital_usd),
                realistic_executable_edge_pct=float(edge_pct),
                execution_fill_probability=float(fill_prob) if fill_prob is not None else 1.0,
                detected_at=detected_at.timestamp(),
                id=id_,
            )
        )
    return opportunities
