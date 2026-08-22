import pytest

from app.config.constants import Strategy
from app.onchain.cross_dex_arbitrage import evaluate_dex_capital_tier
from app.onchain.flash_loan_research import (
    AAVE_V3_FLASH_LOAN_FEE_PCT,
    build_flash_loan_opportunity,
    compare_own_capital_vs_flash_loan,
    find_best_flash_loan_size,
    simulate_flash_loan_tier,
)
from app.onchain.models import DexPool


def _pool(dex, t0, t1, price, tvl=50_000_000.0, fee=0.05, chain="eth") -> DexPool:
    return DexPool(
        chain=chain, dex=dex, pool_id=f"{chain}_{dex}", token0_symbol=t0, token1_symbol=t1,
        price=price, tvl_usd=tvl, volume_24h_usd=1_000_000.0, fee_pct=fee, pool_created_at=None, last_update=1_800_000_000.0,
    )


def _venues():
    return _pool("uniswap_v3", "ETH", "USDC", 2500.0), _pool("sushiswap", "ETH", "USDC", 2525.0)  # 1% richer


def test_flash_loan_fee_is_the_real_aave_v3_rate():
    buy_pool, sell_pool = _venues()
    result = simulate_flash_loan_tier(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0, borrowed_capital_usd=10_000.0)
    assert result.flash_loan_fee_usd == pytest.approx(10_000.0 * (AAVE_V3_FLASH_LOAN_FEE_PCT / 100))
    assert result.flash_loan_fee_usd == pytest.approx(5.0)


def test_final_net_profit_is_arbitrage_profit_minus_loan_fee():
    buy_pool, sell_pool = _venues()
    result = simulate_flash_loan_tier(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0, borrowed_capital_usd=10_000.0)
    assert result.final_net_profit_usd == pytest.approx(result.arbitrage_net_profit_usd - result.flash_loan_fee_usd)


def test_a_profitable_moderate_size_is_repayable():
    buy_pool, sell_pool = _venues()
    result = simulate_flash_loan_tier(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0, borrowed_capital_usd=50_000.0)
    assert result.repayable is True
    assert result.final_net_profit_usd == pytest.approx(148.9681510765062, rel=1e-6)


def test_a_size_too_large_for_the_pool_depth_is_not_repayable():
    """Spec section 9's own rule: if the transaction cannot repay
    principal + fees, it is NOT EXECUTABLE — proven with a size where AMM
    impact eats the entire arbitrage edge and then some."""
    buy_pool, sell_pool = _venues()
    result = simulate_flash_loan_tier(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0, borrowed_capital_usd=500_000.0)
    assert result.repayable is False
    assert result.final_net_profit_usd < 0


def test_find_best_flash_loan_size_picks_the_dollar_optimal_repayable_size():
    buy_pool, sell_pool = _venues()
    best = find_best_flash_loan_size(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0)
    assert best is not None
    assert best.borrowed_capital_usd == 50_000.0
    assert best.repayable is True


def test_find_best_flash_loan_size_never_returns_an_unrepayable_result_even_if_it_looks_bigger():
    buy_pool, sell_pool = _venues()
    best = find_best_flash_loan_size(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0)
    assert best.final_net_profit_usd > 0
    assert best.repayable is True


def test_find_best_flash_loan_size_returns_none_when_nothing_is_repayable():
    # No real price gap at all — every size just loses to fees.
    buy_pool = _pool("uniswap_v3", "ETH", "USDC", 2500.0)
    sell_pool = _pool("sushiswap", "ETH", "USDC", 2500.0)
    assert find_best_flash_loan_size(buy_pool, sell_pool, 2500.0, 2500.0, gas_cost_usd=5.0) is None


def test_compare_own_capital_vs_flash_loan_can_find_flash_loan_superior():
    buy_pool, sell_pool = _venues()
    own_capital = evaluate_dex_capital_tier(buy_pool, sell_pool, 2500.0, 2525.0, capital_usd=1_000.0, gas_cost_usd=5.0)
    flash_loan = find_best_flash_loan_size(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0)
    comparison = compare_own_capital_vs_flash_loan(own_capital, flash_loan)
    assert comparison.flash_loan_is_superior is True
    assert comparison.flash_loan_net_profit_usd > comparison.own_capital_net_profit_usd


def test_build_flash_loan_opportunity_tags_the_flash_loan_strategy():
    buy_pool, sell_pool = _venues()
    result = simulate_flash_loan_tier(buy_pool, sell_pool, 2500.0, 2525.0, gas_cost_usd=5.0, borrowed_capital_usd=50_000.0)
    opp = build_flash_loan_opportunity(buy_pool, sell_pool, result, theoretical_edge_pct=1.0)
    assert opp.strategy == Strategy.FLASH_LOAN_RESEARCH
    assert opp.capital_usd == 50_000.0
    assert opp.expected_profit_usd == pytest.approx(result.final_net_profit_usd)


def test_compare_own_capital_vs_flash_loan_handles_no_viable_flash_loan_size():
    buy_pool = _pool("uniswap_v3", "ETH", "USDC", 2500.0)
    sell_pool = _pool("sushiswap", "ETH", "USDC", 2500.0)
    own_capital = evaluate_dex_capital_tier(buy_pool, sell_pool, 2500.0, 2500.0, capital_usd=1_000.0, gas_cost_usd=5.0)
    comparison = compare_own_capital_vs_flash_loan(own_capital, None)
    assert comparison.flash_loan_is_superior is False
    assert comparison.flash_loan_net_profit_usd is None
