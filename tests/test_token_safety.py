from datetime import UTC, datetime, timedelta

from app.onchain.models import DexPool
from app.onchain.token_safety import MIN_TOKEN_SAFETY_SCORE, compute_token_safety_score, is_token_safety_acceptable


def _pool(**overrides) -> DexPool:
    defaults = dict(
        chain="eth", dex="uniswap_v3", pool_id="eth_0xabc", token0_symbol="USDC", token1_symbol="WETH",
        price=0.0004, tvl_usd=5_000_000.0, volume_24h_usd=1_000_000.0, fee_pct=0.05,
        pool_created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365), last_update=1_700_000_000.0,
    )
    defaults.update(overrides)
    return DexPool(**defaults)


def test_deep_liquid_established_major_pair_scores_high():
    score = compute_token_safety_score(_pool())
    assert score >= MIN_TOKEN_SAFETY_SCORE
    assert is_token_safety_acceptable(_pool()) is True


def test_non_major_pair_loses_the_largest_scoring_component():
    major_pool = _pool()
    non_major_pool = _pool(token1_symbol="RANDOMSHITCOIN")
    assert compute_token_safety_score(non_major_pool) < compute_token_safety_score(major_pool)


def test_thin_pool_scores_lower_than_a_deep_one():
    deep = _pool(tvl_usd=10_000_000.0)
    thin = _pool(tvl_usd=1_000.0)
    assert compute_token_safety_score(thin) < compute_token_safety_score(deep)


def test_brand_new_pool_scores_lower_than_an_established_one():
    established = _pool(pool_created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365))
    brand_new = _pool(pool_created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1))
    assert compute_token_safety_score(brand_new) < compute_token_safety_score(established)


def test_unknown_age_is_scored_neutral_not_penalized_to_zero():
    known_old = compute_token_safety_score(_pool(pool_created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(days=365)))
    unknown = compute_token_safety_score(_pool(pool_created_at=None))
    brand_new = compute_token_safety_score(_pool(pool_created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)))
    assert brand_new < unknown < known_old


def test_score_is_always_bounded_0_to_1():
    extreme = _pool(tvl_usd=10**12, volume_24h_usd=10**12, token1_symbol="?")
    score = compute_token_safety_score(extreme)
    assert 0.0 <= score <= 1.0


def test_a_shady_thin_new_non_major_pool_is_rejected():
    shady = _pool(
        token1_symbol="SCAMCOIN", tvl_usd=500.0, volume_24h_usd=10.0,
        pool_created_at=datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=2),
    )
    assert is_token_safety_acceptable(shady) is False
