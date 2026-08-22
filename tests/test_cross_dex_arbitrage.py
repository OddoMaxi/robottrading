import pytest

from app.config.constants import Strategy
from app.onchain.cross_dex_arbitrage import (
    compute_dex_depth_adjusted_edge,
    detect_cross_dex_opportunity,
    estimate_amm_output_usd,
    evaluate_dex_capital_tier,
    order_buy_sell_pools,
)
from app.onchain.mev_risk import compute_mev_risk_score, mev_buffer_pct_for_risk
from app.onchain.models import DexPool

NOW = 1_800_000_000.0


def _pool(dex, token0, token1, price, tvl_usd=5_000_000.0, fee_pct=0.25, chain="solana") -> DexPool:
    return DexPool(
        chain=chain, dex=dex, pool_id=f"{chain}_{dex}", token0_symbol=token0, token1_symbol=token1,
        price=price, tvl_usd=tvl_usd, volume_24h_usd=1_000_000.0, fee_pct=fee_pct, pool_created_at=None, last_update=NOW,
    )


def test_estimate_amm_output_matches_the_constant_product_formula():
    # $500k against a $1M pool (reserve side = $500k) => exactly half.
    assert estimate_amm_output_usd(500_000, 1_000_000) == pytest.approx(250_000)
    assert estimate_amm_output_usd(0, 1_000_000) == 0.0
    assert estimate_amm_output_usd(100, 0) == 0.0


def test_evaluate_capital_tier_requires_the_cross_pool_price_to_capture_any_profit():
    """Regression for a real bug caught building this feature: an earlier
    version of this formula only modeled each pool's OWN impact and never
    multiplied by the actual price difference between the two pools — with
    two pools quoting the SAME rate (no real arbitrage), profit must be
    exactly zero (before costs); this only holds if the formula is
    correctly using buy_price/sell_price, not silently ignoring them."""
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0)
    pool_b = _pool("orca", "SOL", "USDC", price=100.0)  # identical price — no real edge
    result = evaluate_dex_capital_tier(pool_a, pool_b, buy_price=100.0, sell_price=100.0, capital_usd=100.0, gas_cost_usd=0.0)
    # Same price on both sides: net_profit_usd must be exactly -(fees + slippage buffer + MEV buffer), nothing more —
    # any leftover beyond that would mean the price difference leaked into the result despite there being none.
    mev_risk = compute_mev_risk_score(pool_a.chain, 100.0, min(pool_a.tvl_usd, pool_b.tvl_usd))
    expected_costs = 100.0 * (pool_a.fee_pct / 100) + 100.0 * (pool_b.fee_pct / 100) + 100.0 * (0.05 / 100) + 100.0 * (mev_buffer_pct_for_risk(mev_risk) / 100)
    # abs=0.01: fees are computed on the post-AMM-impact filled amount, not
    # raw capital_usd, so this hand-derived figure is a close approximation
    # rather than bit-exact — still tight enough to prove no leftover
    # "profit" from a price difference that doesn't exist here.
    assert result.net_profit_usd == pytest.approx(-expected_costs, abs=0.01)


def test_a_real_price_gap_produces_real_profit_at_a_reasonable_size():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0, fee_pct=0.25)
    pool_b = _pool("orca", "SOL", "USDC", price=101.0, fee_pct=0.30)  # 1% richer
    result = evaluate_dex_capital_tier(pool_a, pool_b, buy_price=100.0, sell_price=101.0, capital_usd=1_000.0, gas_cost_usd=0.004)
    assert result.net_profit_usd == pytest.approx(2.440656771079279, rel=1e-6)
    assert result.net_pct == pytest.approx(0.24406567710792787, rel=1e-6)


def test_optimal_size_maximizes_absolute_profit_and_is_interior_not_the_largest_tier():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0, fee_pct=0.25)
    pool_b = _pool("orca", "SOL", "USDC", price=101.0, fee_pct=0.30)
    edge = compute_dex_depth_adjusted_edge(pool_a, pool_b, buy_price=100.0, sell_price=101.0, gas_cost_usd=0.004, theoretical_edge_pct=1.0)
    assert edge.optimal_capital_usd == 1_000
    assert edge.optimal_net_profit_usd == pytest.approx(2.440656771079279, rel=1e-6)
    assert edge.max_profitable_capital_usd is not None
    assert edge.optimal_capital_usd < edge.max_profitable_capital_usd < 5_000  # 5000 is already a tested loss


def test_detect_cross_dex_opportunity_end_to_end_with_a_real_price_gap():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0, fee_pct=0.25)
    pool_b = _pool("orca", "SOL", "USDC", price=101.0, fee_pct=0.30)
    opp = detect_cross_dex_opportunity(pool_a, pool_b, gas_cost_usd_a=0.002, gas_cost_usd_b=0.002)
    assert opp is not None
    assert opp.strategy == Strategy.DEX_CROSS
    assert opp.symbol == "SOL/USDC"
    assert opp.legs[0]["exchange"] == "raydium" and opp.legs[0]["side"] == "buy"
    assert opp.legs[1]["exchange"] == "orca" and opp.legs[1]["side"] == "sell"
    assert opp.gross_spread_pct == pytest.approx(1.0)
    assert opp.optimal_capital_usd == 1_000
    assert opp.capital_usd == 1_000
    assert opp.market_data_age_seconds >= 0.0  # never negative even with a synthetic future last_update in other fixtures


def test_no_price_difference_is_not_an_opportunity():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0)
    pool_b = _pool("orca", "SOL", "USDC", price=100.0)
    assert detect_cross_dex_opportunity(pool_a, pool_b, 0.002, 0.002) is None


def test_different_chains_are_never_compared():
    pool_a = _pool("uniswap_v3", "ETH", "USDC", price=2_500.0, chain="eth")
    pool_b = _pool("pancakeswap-v3-bsc", "ETH", "USDC", price=2_520.0, chain="bsc")
    assert detect_cross_dex_opportunity(pool_a, pool_b, 5.0, 0.3) is None


def test_token_order_flipped_between_pools_is_still_correctly_normalized():
    # pool_b lists the pair as USDC/SOL instead of SOL/USDC — same
    # conceptual pair, price must be inverted correctly before comparing.
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0)  # 1 SOL = 100 USDC
    pool_b = _pool("orca", "USDC", "SOL", price=1 / 101.0)  # 1 USDC = 1/101 SOL => 1 SOL = 101 USDC
    opp = detect_cross_dex_opportunity(pool_a, pool_b, 0.002, 0.002)
    assert opp is not None
    assert opp.gross_spread_pct == pytest.approx(1.0, rel=1e-3)


def test_a_thin_edge_that_never_clears_real_costs_is_not_an_opportunity():
    """The exact 'don't lower standards' guarantee: a raw spread that
    looks positive but never nets above MIN_NET_EDGE_PCT after fees/gas/
    slippage/MEV at any tested size must be rejected outright, never
    reported as a smaller-but-still-executable opportunity."""
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0, fee_pct=0.25)
    pool_b = _pool("orca", "SOL", "USDC", price=100.05, fee_pct=0.30)  # only 0.05% raw gap
    assert detect_cross_dex_opportunity(pool_a, pool_b, gas_cost_usd_a=0.002, gas_cost_usd_b=0.002) is None


def test_a_non_capturable_chain_rejects_even_a_genuinely_profitable_gap(monkeypatch):
    """Spec section 14: a real price gap that can't realistically be
    captured on-chain (expected inclusion time exceeds the chain's own
    expected opportunity lifetime) must be rejected outright, regardless
    of how profitable it looks on paper."""
    import app.onchain.cross_dex_arbitrage as module

    class _NeverCapturable:
        def is_capturable(self):
            return False

    monkeypatch.setattr(module, "build_execution_model", lambda chain: _NeverCapturable())
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0, fee_pct=0.25)
    pool_b = _pool("orca", "SOL", "USDC", price=101.0, fee_pct=0.30)
    assert detect_cross_dex_opportunity(pool_a, pool_b, gas_cost_usd_a=0.002, gas_cost_usd_b=0.002) is None


def test_order_buy_sell_pools_identifies_the_cheaper_venue_as_buy():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0)
    pool_b = _pool("orca", "SOL", "USDC", price=101.0)
    ordered = order_buy_sell_pools(pool_a, pool_b)
    assert ordered is not None
    buy_pool, sell_pool, buy_price, sell_price, theoretical_edge_pct = ordered
    assert buy_pool.dex == "raydium"
    assert sell_pool.dex == "orca"
    assert theoretical_edge_pct == pytest.approx(1.0)


def test_order_buy_sell_pools_returns_none_for_no_real_difference():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0)
    pool_b = _pool("orca", "SOL", "USDC", price=100.0)
    assert order_buy_sell_pools(pool_a, pool_b) is None


def test_zero_capital_tier_never_divides_by_zero():
    pool_a = _pool("raydium", "SOL", "USDC", price=100.0)
    pool_b = _pool("orca", "SOL", "USDC", price=101.0)
    result = evaluate_dex_capital_tier(pool_a, pool_b, buy_price=100.0, sell_price=101.0, capital_usd=0.0, gas_cost_usd=0.0)
    assert result.net_pct == 0.0
