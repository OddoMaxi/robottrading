"""Capital Velocity Score & Return per Minute (Fast-Rotation spec, sections 13-14).

Ranks opportunities by how efficiently they use capital and time, not just
by raw profit — a small, fast, high-probability trade can beat a bigger,
slower one once capital *reuse* is the actual objective (spec section 15:
rank by Net Profit x Execution Probability x Capital Velocity, not profit
alone).

Like the Opportunity Score (app.opportunity.scorer), this is an explicit,
documented deterministic formula — a starting point pending recalibration
once real execution data exists, not a measurement.

Weights match the user's own explicit priority order (FAST ROTATION &
CAPITAL VELOCITY OPTIMIZER, 2026-08-21): 1. realistic net profit,
2. execution probability, 3. capital efficiency, 4. short holding time,
5. frequency. "Frequency" isn't scored here — it's a property of how
often the market creates a given edge, observable only in aggregate
across many detections, not something a single opportunity's own fields
can express; scoring higher on this formula just means an opportunity is
individually a better use of capital *right now*, which is the honest
scope of a per-opportunity ranking function. The previous version's
separate "release_speed" factor was dropped — it was, by its own
docstring, mathematically identical to holding_time_score, so it was
double-weighting the same signal under two names rather than adding one.
"""

from dataclasses import dataclass

WEIGHTS = {"profit": 0.35, "probability": 0.25, "capital": 0.20, "holding_time": 0.15, "liquidity": 0.05}

# Reference scales used to normalize raw values into 0-1 sub-scores.
PROFIT_REFERENCE_USD = 20.0  # a $20 profit on a single trade scores full marks on that factor
HOLDING_TIME_REFERENCE_SECONDS = 300.0  # 5 minutes — beyond this, the time score bottoms out at 0
CAPITAL_REFERENCE_USD = 10_000.0  # capital required beyond this scores 0 (harder to redeploy elsewhere)


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def return_per_minute(net_return_pct: float, holding_time_seconds: float) -> float:
    """Net return normalized by holding time — spec section 13's tie-breaker
    between a big-but-slow trade and a small-but-fast one."""
    holding_minutes = max(holding_time_seconds / 60.0, 1e-9)
    return net_return_pct / holding_minutes


@dataclass(slots=True)
class VelocityFactors:
    profit_score: float
    probability_score: float
    holding_time_score: float
    capital_score: float
    liquidity_score: float


def capital_velocity_score(
    net_profit_usd: float,
    execution_probability: float,
    holding_time_seconds: float,
    capital_usd: float,
    liquidity_score: float = 0.5,
) -> tuple[float, VelocityFactors]:
    """0-100. `liquidity_score` (0-1) is caller-supplied — neutral (0.5) if not otherwise known."""
    profit_score = _clamp01(net_profit_usd / PROFIT_REFERENCE_USD) if net_profit_usd > 0 else 0.0
    probability_score = _clamp01(execution_probability)
    holding_time_score = _clamp01(1 - holding_time_seconds / HOLDING_TIME_REFERENCE_SECONDS)
    capital_score = _clamp01(1 - capital_usd / CAPITAL_REFERENCE_USD)

    factors = VelocityFactors(profit_score, probability_score, holding_time_score, capital_score, liquidity_score)
    raw = (
        WEIGHTS["profit"] * factors.profit_score
        + WEIGHTS["probability"] * factors.probability_score
        + WEIGHTS["capital"] * factors.capital_score
        + WEIGHTS["holding_time"] * factors.holding_time_score
        + WEIGHTS["liquidity"] * factors.liquidity_score
    )
    return round(raw * 100, 1), factors
