"""Maker Fill Probability (Net Opportunity Engine spec, section 4).

We have no real historical fill-rate data for these exchanges — that needs
a testnet/live run to measure. This is an explicit, documented heuristic
combining the four proxies the spec calls for, not a measurement:
  - how tight the spread is (tighter -> more natural order flow crossing
    the touch -> more likely a resting order gets filled)
  - how much size is already resting at the touch relative to our order
    (more depth -> more evidence of real, active two-sided interest there)
  - recent volatility (some price churn helps a resting order get crossed;
    modeled as a simple lightweight in-memory estimate — see
    app.market_data.store.MarketDataStore.recent_volatility_pct)
  - historical spread duration — not tracked yet (needs the Duration
    Engine), held at a neutral 0.5 until then

Every score is clamped to [MIN, MAX] — this never claims certainty either way.
"""

from dataclasses import dataclass

MIN_FILL_PROBABILITY = 0.05
MAX_FILL_PROBABILITY = 0.95

# Relative spread beyond which the spread-tightness score bottoms out at 0.
SPREAD_REFERENCE_PCT = 0.50
# Recent volatility beyond which the volatility score saturates at 1.
VOLATILITY_REFERENCE_PCT = 0.10
# Touch depth this many times our order size scores full liquidity confidence.
LIQUIDITY_TARGET_MULTIPLE = 3.0

WEIGHTS = {"spread": 0.35, "liquidity": 0.30, "volatility": 0.20, "duration": 0.15}


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


@dataclass(slots=True)
class FillProbabilityFactors:
    spread_score: float
    liquidity_score: float
    volatility_score: float
    duration_score: float


def estimate_maker_fill_probability(
    spread_pct: float,
    touch_quantity_usd: float,
    order_size_usd: float,
    recent_volatility_pct: float | None,
) -> tuple[float, FillProbabilityFactors]:
    spread_score = _clamp01(1 - spread_pct / SPREAD_REFERENCE_PCT)
    liquidity_score = _clamp01(touch_quantity_usd / (order_size_usd * LIQUIDITY_TARGET_MULTIPLE)) if order_size_usd > 0 else 0.0
    volatility_score = _clamp01(recent_volatility_pct / VOLATILITY_REFERENCE_PCT) if recent_volatility_pct is not None else 0.5
    duration_score = 0.5  # placeholder pending the Duration Engine

    factors = FillProbabilityFactors(spread_score, liquidity_score, volatility_score, duration_score)
    raw = (
        WEIGHTS["spread"] * factors.spread_score
        + WEIGHTS["liquidity"] * factors.liquidity_score
        + WEIGHTS["volatility"] * factors.volatility_score
        + WEIGHTS["duration"] * factors.duration_score
    )
    return max(MIN_FILL_PROBABILITY, min(MAX_FILL_PROBABILITY, raw)), factors
