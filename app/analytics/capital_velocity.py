"""Capital Velocity Score & Return per Minute (Fast-Rotation spec, sections 13-14).

Ranks opportunities by how efficiently they use capital and time, not just
by raw profit — a small, fast, high-probability trade can beat a bigger,
slower one once capital *reuse* is the actual objective (spec section 15:
rank by Net Profit x Execution Probability x Capital Velocity, not profit
alone).

Like the Opportunity Score (app.opportunity.scorer), this is an explicit,
documented deterministic formula — a starting point pending recalibration
once real execution data exists, not a measurement.
"""

from dataclasses import dataclass

WEIGHTS = {"profit": 0.25, "probability": 0.20, "holding_time": 0.25, "capital": 0.10, "liquidity": 0.10, "release_speed": 0.10}

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
    release_speed_score: float


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
    # Capital frees up exactly when the position closes, so "how fast does it
    # release" carries the same signal as "how short is the hold".
    release_speed_score = holding_time_score

    factors = VelocityFactors(profit_score, probability_score, holding_time_score, capital_score, liquidity_score, release_speed_score)
    raw = (
        WEIGHTS["profit"] * factors.profit_score
        + WEIGHTS["probability"] * factors.probability_score
        + WEIGHTS["holding_time"] * factors.holding_time_score
        + WEIGHTS["capital"] * factors.capital_score
        + WEIGHTS["liquidity"] * factors.liquidity_score
        + WEIGHTS["release_speed"] * factors.release_speed_score
    )
    return round(raw * 100, 1), factors
