import pytest

from app.onchain.constants import MEV_BUFFER_PCT
from app.onchain.mev_risk import compute_mev_risk_score, mev_buffer_pct_for_risk


def test_tiny_trade_against_a_deep_pool_scores_low_risk():
    score = compute_mev_risk_score("solana", trade_size_usd=100.0, pool_tvl_usd=10_000_000.0)
    assert score < 0.5


def test_large_trade_relative_to_pool_scores_high_risk():
    score = compute_mev_risk_score("eth", trade_size_usd=500_000.0, pool_tvl_usd=1_000_000.0)  # 50% of pool
    assert score > 0.9


def test_ethereum_scores_higher_mev_risk_than_solana_at_the_same_relative_size():
    eth_score = compute_mev_risk_score("eth", trade_size_usd=100.0, pool_tvl_usd=10_000_000.0)
    sol_score = compute_mev_risk_score("solana", trade_size_usd=100.0, pool_tvl_usd=10_000_000.0)
    assert eth_score > sol_score


def test_score_is_always_bounded_0_to_1():
    score = compute_mev_risk_score("eth", trade_size_usd=10_000_000.0, pool_tvl_usd=1_000.0)  # absurdly large trade
    assert 0.0 <= score <= 1.0


def test_zero_tvl_pool_is_treated_as_maximum_size_risk_not_a_crash():
    score = compute_mev_risk_score("eth", trade_size_usd=100.0, pool_tvl_usd=0.0)
    assert score > 0.0


def test_mev_buffer_never_drops_below_the_documented_floor():
    assert mev_buffer_pct_for_risk(0.0) == pytest.approx(MEV_BUFFER_PCT)


def test_mev_buffer_scales_up_with_risk_score():
    low = mev_buffer_pct_for_risk(0.1)
    high = mev_buffer_pct_for_risk(0.9)
    assert high > low
    assert high <= MEV_BUFFER_PCT * 3.0 + 1e-9


def test_mev_buffer_clamps_out_of_range_risk_scores():
    assert mev_buffer_pct_for_risk(-1.0) == pytest.approx(MEV_BUFFER_PCT)
    assert mev_buffer_pct_for_risk(5.0) == pytest.approx(MEV_BUFFER_PCT * 3.0)
