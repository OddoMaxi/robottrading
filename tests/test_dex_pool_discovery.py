from datetime import UTC, datetime, timedelta

from app.onchain.models import DexPool
from app.onchain.pool_discovery import filter_eligible_pools


def _pool(**overrides) -> DexPool:
    defaults = dict(
        chain="eth",
        dex="uniswap_v3",
        pool_id="eth_0xabc",
        token0_symbol="USDC",
        token1_symbol="WETH",
        price=0.0004,
        tvl_usd=5_000_000.0,
        volume_24h_usd=1_000_000.0,
        fee_pct=0.05,
        pool_created_at=None,
        last_update=1_700_000_000.0,
    )
    defaults.update(overrides)
    return DexPool(**defaults)


def test_liquid_whitelisted_pool_passes():
    assert filter_eligible_pools([_pool()]) == [_pool()]


def test_low_tvl_pool_is_filtered_out():
    assert filter_eligible_pools([_pool(tvl_usd=1_000.0)]) == []


def test_unknown_token_pool_is_filtered_out():
    assert filter_eligible_pools([_pool(token1_symbol="RANDOMSHITCOIN")]) == []


def test_zero_price_pool_is_filtered_out():
    assert filter_eligible_pools([_pool(price=0.0)]) == []


def test_pool_with_no_age_data_is_not_penalized():
    # pool_created_at missing from the source data (not every DEX/pool
    # exposes it) must not be treated as "too new" by default.
    assert filter_eligible_pools([_pool(pool_created_at=None)]) == [_pool(pool_created_at=None)]


def test_freshly_created_pool_is_filtered_out():
    brand_new = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=1)
    assert filter_eligible_pools([_pool(pool_created_at=brand_new)]) == []


def test_established_pool_passes_the_age_gate():
    established = datetime.now(UTC).replace(tzinfo=None) - timedelta(days=30)
    pool = _pool(pool_created_at=established)
    assert filter_eligible_pools([pool]) == [pool]
