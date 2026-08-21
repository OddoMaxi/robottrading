"""Latency Engine + pre-execution revalidation (Reality Engine spec, sections 10-11).

Detection and order submission aren't instantaneous — market data has to
be received, a decision made, an order sent, acknowledged, and filled.
Real time passes between the price an opportunity was priced at and the
price it can actually execute against. This models that gap and
revalidates the opportunity against a simulated price move over the
elapsed latency: if the edge no longer clears break-even by the time
execution could plausibly happen, the trade is MISSED — never credit the
pre-latency profit for a spread that had already moved.
"""

import random
from dataclasses import dataclass
from enum import StrEnum


class LatencyProfile(StrEnum):
    OPTIMISTIC = "optimistic"
    REALISTIC = "realistic"
    STRESS = "stress"


# (min_ms, max_ms) for the *total* round-trip latency in each profile —
# spec section 10's own ranges.
PROFILE_RANGES_MS: dict[LatencyProfile, tuple[float, float]] = {
    LatencyProfile.OPTIMISTIC: (20.0, 50.0),
    LatencyProfile.REALISTIC: (80.0, 250.0),
    LatencyProfile.STRESS: (300.0, 700.0),
}

DEFAULT_PROFILE = LatencyProfile.REALISTIC

# How the total sampled latency splits across the six named stages (section
# 10) — proportions of the total, not independently-sampled ranges, so the
# stages always sum to exactly the sampled total.
STAGE_PROPORTIONS: dict[str, float] = {
    "market_data_ms": 0.15,
    "internal_processing_ms": 0.10,
    "decision_ms": 0.10,
    "order_submission_ms": 0.20,
    "exchange_ack_ms": 0.25,
    "fill_ms": 0.20,
}


@dataclass(slots=True)
class LatencyBreakdown:
    profile: LatencyProfile
    total_ms: float
    market_data_ms: float
    internal_processing_ms: float
    decision_ms: float
    order_submission_ms: float
    exchange_ack_ms: float
    fill_ms: float


def sample_latency(profile: LatencyProfile = DEFAULT_PROFILE, rng: random.Random | None = None) -> LatencyBreakdown:
    rng = rng or random.Random()
    low, high = PROFILE_RANGES_MS[profile]
    total_ms = rng.uniform(low, high)
    return LatencyBreakdown(profile=profile, total_ms=total_ms, **{field: total_ms * share for field, share in STAGE_PROPORTIONS.items()})


# A conservative, documented-as-approximate baseline for how much a crypto
# spot price typically moves per second of elapsed time. Real volatility
# varies a lot by asset, hour, and market condition — this is a
# placeholder until it's wired to each opportunity's own recently-observed
# volatility (app.market_data.store.MarketDataStore.recent_volatility_pct),
# deliberately small enough that it doesn't dominate a healthy spread's
# margin, the same way real sub-second latency mostly doesn't.
DEFAULT_VOLATILITY_PCT_PER_SQRT_SECOND = 0.02


def simulate_price_impact(
    latency_ms: float, rng: random.Random, volatility_pct_per_sqrt_second: float = DEFAULT_VOLATILITY_PCT_PER_SQRT_SECOND
) -> float:
    """A random price move (can help or hurt) over `latency_ms`, scaled by
    sqrt(elapsed time) the way a random walk's standard deviation grows —
    twice the wait isn't twice the expected move, it's sqrt(2) times."""
    elapsed_seconds = latency_ms / 1000.0
    std_pct = volatility_pct_per_sqrt_second * (elapsed_seconds**0.5)
    return rng.gauss(0.0, std_pct)


@dataclass(slots=True)
class RevalidationResult:
    still_valid: bool
    original_net_spread_pct: float
    revalidated_net_spread_pct: float
    price_move_pct: float
    latency: LatencyBreakdown


def revalidate_after_latency(
    net_spread_pct: float,
    break_even_pct: float,
    profile: LatencyProfile = DEFAULT_PROFILE,
    rng: random.Random | None = None,
) -> RevalidationResult:
    """Section 11's worked example: recompute the opportunity's edge after
    simulated latency: still_valid is False exactly when the revalidated
    edge has dropped below break-even, in which case the trade must be
    reported MISSED, not executed at its original priced profit."""
    rng = rng or random.Random()
    latency = sample_latency(profile, rng)
    price_move_pct = simulate_price_impact(latency.total_ms, rng)
    revalidated_net_spread_pct = net_spread_pct + price_move_pct
    return RevalidationResult(
        still_valid=revalidated_net_spread_pct >= break_even_pct,
        original_net_spread_pct=net_spread_pct,
        revalidated_net_spread_pct=revalidated_net_spread_pct,
        price_move_pct=price_move_pct,
        latency=latency,
    )
