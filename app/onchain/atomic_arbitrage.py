"""Atomic Arbitrage Research (Multi-Market Opportunity Engine, V5.5, spec section 8).

"All legs execute in one blockchain transaction. If any leg fails: entire
transaction reverts." Models the specific economic trade-off atomic
bundling buys over the sequential per-leg execution
app.onchain.cross_dex_arbitrage/multihop_arbitrage already price: NO
unhedged multi-leg position risk (a revert means NOTHING happened — no
CEX-style EMERGENCY_UNWIND scenario is even possible) — at the cost of
paying gas even on a revert (most EVM chains still charge gas for a
reverted transaction) and zero partial profit from an attempt that reverts.

Research/simulation only, per the spec's own section heading — this NEVER
constructs, signs, or broadcasts an actual bundled transaction; no such
capability exists anywhere in this codebase (spec section 33 — "No real
flash-loan transaction... this version is LIVE DATA + SHADOW EXECUTION +
REALISTIC SIMULATION").

"Do not assume guaranteed block inclusion" cuts both ways: a bundle is
never assumed to succeed AND never assumed to always revert. revert
probability is a documented, conservative ASSUMPTION scaled by the same
chain MEV-competitiveness signal app.onchain.mev_risk already uses (a more
MEV-competitive chain means more attempts to front-run/reorder around your
bundle, which is what actually trips a slippage-protected atomic bundle's
revert condition before inclusion) — NOT a measurement; no historical
atomic-bundle outcome data exists yet to calibrate this against (same
caveat as app.onchain.execution_model's expected_opportunity_lifetime_seconds).
"""

import uuid
from dataclasses import dataclass, replace

from app.config.constants import Strategy
from app.onchain.mev_risk import chain_mev_competitiveness_score
from app.opportunity.models import Opportunity

MIN_REVERT_PROBABILITY = 0.02
MAX_REVERT_PROBABILITY = 0.25


@dataclass(slots=True)
class AtomicSimulationResult:
    strategy: str
    symbol: str
    revert_probability: float
    success_net_profit_usd: float  # what you get if the bundle lands
    revert_cost_usd: float  # gas paid even on a revert — the only possible loss (never principal)
    expected_value_usd: float  # probability-weighted: (1-p)*success_profit - p*revert_cost


def estimate_revert_probability(chain: str, num_legs: int) -> float:
    """Scales between the documented floor/ceiling by chain MEV
    competitiveness AND leg count — more legs means more price-sensitive
    conditions that all have to hold simultaneously at inclusion time for
    the bundle's slippage protection not to trip."""
    chain_score = chain_mev_competitiveness_score(chain)
    leg_factor = min(1.0, (num_legs - 1) / 3.0)  # 2 legs -> ~0.33, 4 legs -> 1.0 of the leg-count contribution
    combined = 0.6 * chain_score + 0.4 * leg_factor
    return MIN_REVERT_PROBABILITY + combined * (MAX_REVERT_PROBABILITY - MIN_REVERT_PROBABILITY)


def simulate_atomic_bundle(opportunity: Opportunity, gas_cost_usd: float) -> AtomicSimulationResult | None:
    """Takes an already-detected, already cost-priced Opportunity (from
    cross_dex_arbitrage or multihop_arbitrage — this module never detects
    an opportunity on its own, it only re-prices an existing one under the
    atomic-execution model) and computes its expected value if bundled
    atomically instead of executed leg-by-leg."""
    if opportunity.expected_profit_usd is None or opportunity.capital_usd is None:
        return None
    chain = opportunity.legs[0].get("chain") if opportunity.legs else None
    if chain is None:
        return None
    num_legs = len(opportunity.legs)

    revert_probability = estimate_revert_probability(chain, num_legs)
    success_net_profit_usd = opportunity.expected_profit_usd
    expected_value_usd = (1 - revert_probability) * success_net_profit_usd - revert_probability * gas_cost_usd

    return AtomicSimulationResult(
        strategy=opportunity.strategy,
        symbol=opportunity.symbol,
        revert_probability=revert_probability,
        success_net_profit_usd=success_net_profit_usd,
        revert_cost_usd=gas_cost_usd,
        expected_value_usd=expected_value_usd,
    )


def as_atomic_opportunity(opportunity: Opportunity, result: AtomicSimulationResult) -> Opportunity:
    """Returns a NEW Opportunity (never mutates the original — the
    sequential-execution version stays valid and unrelated) tagged
    Strategy.ATOMIC, priced at the probability-weighted expected value
    rather than the naive "assume it always lands" success profit."""
    return replace(
        opportunity,
        strategy=Strategy.ATOMIC,
        expected_profit_usd=result.expected_value_usd,
        net_spread_pct=(result.expected_value_usd / opportunity.capital_usd * 100) if opportunity.capital_usd else 0.0,
        # A fresh id — this is a distinct DB row (its own strategy, its own
        # probability-weighted pricing), never the same opportunity_id as
        # the sequential-execution version it was derived from.
        id=uuid.uuid4(),
    )
