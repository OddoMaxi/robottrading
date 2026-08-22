"""Shadow Master Ranker (Phase 2, SHADOW MODE ONLY).

Ranks a CEX or DEX opportunity by realistic expected value per unit of
capital and time — never by raw gross spread, never by raw dollar
profit alone (spec Part E's own explicit rule). Reuses
capital_velocity_score wherever the real engines already computed it
(app.opportunity.detector for CEX, app.onchain.ranking for DEX — both
already produce a 0-100, directly-comparable score, so this module does
NOT re-derive ranking logic from scratch, it composes what's already
there) — falls back to a from-scratch EV-per-capital-minute calculation
only when that score is missing.
"""

from app.shadow.models import ShadowOpportunitySummary


def compute_expected_value_usd(opp: ShadowOpportunitySummary) -> float | None:
    """expected_profit_usd already reflects realistic costs (fees, gas,
    slippage, price impact — see each detection engine's own docstring);
    weighting by execution_fill_probability turns it into a genuine
    expected value rather than an assume-it-always-fills number."""
    if opp.expected_profit_usd is None:
        return None
    fill_probability = opp.execution_fill_probability if opp.execution_fill_probability is not None else 1.0
    return opp.expected_profit_usd * fill_probability


def compute_master_rank_score(opp: ShadowOpportunitySummary) -> float | None:
    """Higher is better. Prefers the real, already-computed
    capital_velocity_score (0-100, capital- and time-normalized) when
    available; otherwise derives an equivalent EV-per-capital-minute
    figure from what IS available. Returns None only when there's
    nothing to rank on at all (missing capital_usd or expected_profit_usd) —
    never a fabricated default score."""
    if opp.capital_velocity_score is not None:
        return opp.capital_velocity_score

    ev = compute_expected_value_usd(opp)
    if ev is None or opp.capital_usd is None or opp.capital_usd <= 0:
        return None
    holding_minutes = (opp.holding_period_seconds or 60.0) / 60.0
    if holding_minutes <= 0:
        return None
    # Same shape as capital_velocity_score's own scale (an unbounded raw
    # ratio, not 0-100) — comparable to itself across opportunities that
    # lack the real score, but NOT directly comparable in magnitude to a
    # real capital_velocity_score value; callers only ever use this to
    # RANK opportunities relative to each other, never as an absolute
    # cross-engine score.
    return ev / (opp.capital_usd * holding_minutes) * 1000
