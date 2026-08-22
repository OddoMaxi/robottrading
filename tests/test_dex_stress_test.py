import random

from app.reporting.dex_stress_test import ADVERSE_COMBO, BASE, STRESS_1, STRESS_2, simulate_stress_scenario


def _opp(price_a=100.0, price_b=101.0, tvl=2_000_000.0, capital_usd=500.0):
    return {
        "legs": [
            {"chain": "solana", "exchange": "raydium", "pool_id": "pool_a", "price": price_a, "tvl_usd": tvl, "fee_pct": 0.25},
            {"chain": "solana", "exchange": "orca", "pool_id": "pool_b", "price": price_b, "tvl_usd": tvl, "fee_pct": 0.25},
        ],
        "capital_usd": capital_usd,
    }


class _NoDriftRng:
    def gauss(self, mu, sigma):
        return 0.0


def test_simulate_stress_scenario_skips_opportunities_without_a_price_snapshot():
    opps = [{"legs": [{"chain": "solana", "exchange": "raydium"}, {"chain": "solana", "exchange": "orca"}], "capital_usd": 500.0}]
    result = simulate_stress_scenario(opps, BASE, base_gas_cost_usd=0.01, rng=_NoDriftRng())
    assert result.n_skipped_unsupported_shape == 1
    assert result.capture_ratio_pct is None


def test_simulate_stress_scenario_base_is_more_profitable_than_higher_stress_tiers():
    opps = [_opp() for _ in range(10)]
    rng = random.Random(42)
    base_result = simulate_stress_scenario(opps, BASE, base_gas_cost_usd=0.01, rng=random.Random(42))
    stress1_result = simulate_stress_scenario(opps, STRESS_1, base_gas_cost_usd=0.01, rng=random.Random(42))
    stress2_result = simulate_stress_scenario(opps, STRESS_2, base_gas_cost_usd=0.01, rng=random.Random(42))
    adverse_result = simulate_stress_scenario(opps, ADVERSE_COMBO, base_gas_cost_usd=0.01, rng=random.Random(42))
    del rng
    assert base_result.total_net_profit_usd >= stress1_result.total_net_profit_usd >= stress2_result.total_net_profit_usd >= adverse_result.total_net_profit_usd


def test_simulate_stress_scenario_a_marginal_edge_can_flip_to_edge_disappeared_under_stress():
    # A very thin edge (0.06% gap) survives BASE (near-zero gas, near-zero
    # drift) but should not reliably survive ADVERSE_COMBO's much larger
    # gas + slippage + latency-driven drift variance.
    opps = [_opp(price_a=100.0, price_b=100.06, capital_usd=100.0) for _ in range(30)]
    base_result = simulate_stress_scenario(opps, BASE, base_gas_cost_usd=0.001, rng=random.Random(7))
    adverse_result = simulate_stress_scenario(opps, ADVERSE_COMBO, base_gas_cost_usd=0.001, rng=random.Random(7))
    assert adverse_result.n_still_profitable <= base_result.n_still_profitable
