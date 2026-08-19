"""Opportunity Score (section 15) — deterministic V1 formula, 0-100.

Each factor is expected pre-normalized to 0-1 (1 = best) by the caller, since
the raw units differ wildly (seconds vs. % vs. USD). The AI-based scorer
mentioned in section 37 (Phase 3) replaces this weighting scheme later, not
its inputs.
"""

from dataclasses import dataclass


@dataclass(slots=True)
class ScoreFactors:
    net_profit: float
    liquidity: float
    duration: float
    volatility: float
    slippage: float
    depth: float
    latency: float
    exchange_risk: float
    execution_complexity: float


DEFAULT_WEIGHTS: dict[str, float] = {
    "net_profit": 0.25,
    "liquidity": 0.15,
    "duration": 0.10,
    "volatility": 0.10,
    "slippage": 0.10,
    "depth": 0.10,
    "latency": 0.10,
    "exchange_risk": 0.05,
    "execution_complexity": 0.05,
}


def score(factors: ScoreFactors, weights: dict[str, float] = DEFAULT_WEIGHTS) -> float:
    total = sum(getattr(factors, name) * weight for name, weight in weights.items())
    return round(total * 100, 2)
