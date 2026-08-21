import random

import pytest

from app.execution.latency_engine import (
    DEFAULT_PROFILE,
    PROFILE_RANGES_MS,
    LatencyProfile,
    revalidate_after_latency,
    sample_latency,
    simulate_price_impact,
)


def test_default_profile_is_realistic():
    assert DEFAULT_PROFILE == LatencyProfile.REALISTIC


@pytest.mark.parametrize("profile", list(LatencyProfile))
def test_sample_latency_stays_within_its_profiles_range(profile):
    low, high = PROFILE_RANGES_MS[profile]
    rng = random.Random(0)
    for _ in range(50):
        latency = sample_latency(profile, rng)
        assert low <= latency.total_ms <= high


def test_sample_latency_stages_sum_to_the_total():
    latency = sample_latency(LatencyProfile.REALISTIC, random.Random(1))
    stage_sum = (
        latency.market_data_ms
        + latency.internal_processing_ms
        + latency.decision_ms
        + latency.order_submission_ms
        + latency.exchange_ack_ms
        + latency.fill_ms
    )
    assert stage_sum == pytest.approx(latency.total_ms)


def test_stress_profile_has_higher_latency_than_optimistic():
    rng = random.Random(2)
    optimistic = sample_latency(LatencyProfile.OPTIMISTIC, rng)
    stress = sample_latency(LatencyProfile.STRESS, rng)
    assert stress.total_ms > optimistic.total_ms


def test_price_impact_scales_with_sqrt_of_elapsed_time():
    rng_a, rng_b = random.Random(5), random.Random(5)  # same seed -> same underlying gauss() draw
    short = simulate_price_impact(100.0, rng_a)
    long = simulate_price_impact(400.0, rng_b)  # 4x the time -> 2x the std dev, same draw
    assert abs(long) == pytest.approx(abs(short) * 2, rel=1e-6)


def test_revalidation_stays_valid_for_a_comfortably_profitable_spread():
    # net edge far above break-even; the tiny latency-driven price move
    # can't plausibly erase that much margin.
    result = revalidate_after_latency(net_spread_pct=0.50, break_even_pct=0.05, profile=LatencyProfile.REALISTIC, rng=random.Random(3))
    assert result.still_valid is True


def test_revalidation_can_invalidate_a_razor_thin_spread():
    """With enough samples, a spread priced right at its break-even floor
    must sometimes fail revalidation — otherwise latency has no teeth."""
    saw_invalid = False
    for seed in range(500):
        result = revalidate_after_latency(net_spread_pct=0.051, break_even_pct=0.05, profile=LatencyProfile.STRESS, rng=random.Random(seed))
        if not result.still_valid:
            saw_invalid = True
            break
    assert saw_invalid


def test_revalidation_result_reports_the_original_and_revalidated_edge():
    result = revalidate_after_latency(net_spread_pct=0.30, break_even_pct=0.05, rng=random.Random(4))
    assert result.original_net_spread_pct == 0.30
    assert result.revalidated_net_spread_pct == pytest.approx(0.30 + result.price_move_pct)
