"""Master Opportunity Ranker (Multi-Market Opportunity Engine, V5.5, spec
sections 17-18).

CEX and DEX opportunities already share one Opportunity type and one
opportunities table (the Opportunity Bus, spec section 1) — ranking them
together is therefore already just "sort by the same score", not a
separate aggregation system to build. app.analytics.capital_velocity's
capital_velocity_score (Fast-Rotation spec, built for CEX) is
strategy-agnostic by construction — net profit, execution probability,
capital required, holding time, liquidity: none of those fields are
CEX-specific — so this module doesn't reimplement scoring, it applies the
SAME formula to every DEX opportunity too. This IS spec section 18's own
instruction ("compare NET PROFIT / CAPITAL-MINUTE, not only absolute
profit") already built and already tested; generalizing where it's
applied, not rewriting it.
"""

from app.analytics.capital_velocity import capital_velocity_score, return_per_minute
from app.opportunity.models import Opportunity


def apply_master_ranking_score(opp: Opportunity, liquidity_score: float = 0.5) -> Opportunity:
    """Mutates opp in place and returns it (same convention as
    app.opportunity.detector.OpportunityDetector._score_velocity uses for
    CEX) — every DEX opportunity gets capital_velocity_score/
    return_per_minute_pct populated on the identical scale a CEX
    opportunity already carries, so a query that ranks "all opportunities"
    together (app.reporting — see the V5.5 completion report for what's
    wired into a dashboard view vs. what's query-ready but not yet
    surfaced there) needs no per-strategy special-casing.

    liquidity_score defaults to neutral (0.5), same default
    capital_velocity_score itself already uses for a caller that doesn't
    have a specific liquidity signal — a DEX opportunity's own pool TVL
    IS a real liquidity signal, but plumbing it through every one of the 4
    builder functions is additive follow-up, not required for the score to
    be meaningful today (profit/probability/capital/holding-time already
    carry most of the ranking signal).
    """
    if opp.holding_period_seconds is None or opp.capital_usd is None or opp.expected_profit_usd is None:
        return opp
    opp.return_per_minute_pct = return_per_minute(opp.net_spread_pct or 0.0, opp.holding_period_seconds)
    opp.capital_velocity_score, _ = capital_velocity_score(
        net_profit_usd=opp.expected_profit_usd,
        execution_probability=opp.execution_fill_probability if opp.execution_fill_probability is not None else 1.0,
        holding_time_seconds=opp.holding_period_seconds,
        capital_usd=opp.capital_usd,
        liquidity_score=liquidity_score,
    )
    return opp
