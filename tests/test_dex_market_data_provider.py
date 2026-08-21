from app.onchain.market_data_provider import _parse_pool


def _row(name, price="2470.87", reserve="5070710.27", vol24h="100667586.76", created="2021-12-30T20:32:10Z", pool_id="eth_0xabc"):
    return {
        "id": pool_id,
        "attributes": {
            "name": name,
            "base_token_price_quote_token": price,
            "reserve_in_usd": reserve,
            "volume_usd": {"h24": vol24h},
            "pool_created_at": created,
        },
    }


def test_parses_a_uniswap_v3_style_pool_with_fee_in_the_name():
    pool = _parse_pool("eth", "uniswap_v3", _row("USDC / WETH 0.01%"), now=1_700_000_000.0)
    assert pool is not None
    assert pool.chain == "eth"
    assert pool.dex == "uniswap_v3"
    assert pool.token0_symbol == "USDC"
    assert pool.token1_symbol == "WETH"
    assert pool.fee_pct == 0.01
    assert pool.tvl_usd == 5070710.27
    assert pool.volume_24h_usd == 100667586.76
    assert pool.pool_created_at is not None


def test_falls_back_to_the_per_dex_default_fee_when_not_in_the_name():
    pool = _parse_pool("solana", "raydium", _row("SOL / USDC"), now=1_700_000_000.0)
    assert pool is not None
    assert pool.fee_pct == 0.25  # raydium's documented default


def test_orca_default_fee_differs_from_raydium():
    pool = _parse_pool("solana", "orca", _row("SOL / USDC"), now=1_700_000_000.0)
    assert pool.fee_pct == 0.30


def test_malformed_name_without_a_slash_is_skipped():
    assert _parse_pool("eth", "uniswap_v3", _row("not-a-pair-name"), now=1_700_000_000.0) is None


def test_missing_price_field_is_skipped():
    row = _row("USDC / WETH")
    del row["attributes"]["base_token_price_quote_token"]
    assert _parse_pool("eth", "uniswap_v3", row, now=1_700_000_000.0) is None


def test_missing_pool_created_at_leaves_it_none_without_crashing():
    row = _row("USDC / WETH", created=None)
    pool = _parse_pool("eth", "uniswap_v3", row, now=1_700_000_000.0)
    assert pool is not None
    assert pool.pool_created_at is None


def test_zero_reserve_and_volume_default_to_zero_not_none():
    row = _row("USDC / WETH", reserve=None, vol24h=None)
    row["attributes"]["reserve_in_usd"] = None
    row["attributes"]["volume_usd"] = {}
    pool = _parse_pool("eth", "uniswap_v3", row, now=1_700_000_000.0)
    assert pool is not None
    assert pool.tvl_usd == 0.0
    assert pool.volume_24h_usd == 0.0
