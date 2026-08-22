import uuid

import pytest

from app.config.constants import Strategy
from app.onchain.atomic_arbitrage import (
    MAX_REVERT_PROBABILITY,
    MIN_REVERT_PROBABILITY,
    as_atomic_opportunity,
    estimate_revert_probability,
    simulate_atomic_bundle,
)
from app.opportunity.models import Opportunity


def _opportunity(**overrides) -> Opportunity:
    defaults = dict(
        strategy=Strategy.DEX_CROSS,
        symbol="SOL/USDC",
        legs=[
            {"chain": "solana", "exchange": "raydium", "side": "buy", "market": "dex", "pool_id": "p1"},
            {"chain": "solana", "exchange": "orca", "side": "sell", "market": "dex", "pool_id": "p2"},
        ],
        gross_spread_pct=1.0,
        net_spread_pct=0.25,
        capital_usd=1_000.0,
        expected_profit_usd=2.5,
        id=uuid.uuid4(),
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def test_revert_probability_is_always_within_the_documented_bounds():
    for chain in ("eth", "bsc", "solana"):
        for legs in (2, 3, 4):
            p = estimate_revert_probability(chain, legs)
            assert MIN_REVERT_PROBABILITY <= p <= MAX_REVERT_PROBABILITY


def test_more_legs_means_higher_revert_probability():
    two_leg = estimate_revert_probability("eth", 2)
    four_leg = estimate_revert_probability("eth", 4)
    assert four_leg > two_leg


def test_ethereum_has_higher_revert_probability_than_solana_at_the_same_leg_count():
    eth_p = estimate_revert_probability("eth", 2)
    sol_p = estimate_revert_probability("solana", 2)
    assert eth_p > sol_p


def test_simulate_atomic_bundle_computes_probability_weighted_expected_value():
    opp = _opportunity()
    result = simulate_atomic_bundle(opp, gas_cost_usd=1.0)
    assert result is not None
    p = result.revert_probability
    expected = (1 - p) * 2.5 - p * 1.0
    assert result.expected_value_usd == pytest.approx(expected)
    assert result.success_net_profit_usd == 2.5
    assert result.revert_cost_usd == 1.0


def test_simulate_atomic_bundle_expected_value_is_always_less_than_the_naive_success_profit():
    """The whole point of modeling revert risk explicitly — never assume
    guaranteed inclusion (spec section 8)."""
    opp = _opportunity()
    result = simulate_atomic_bundle(opp, gas_cost_usd=1.0)
    assert result.expected_value_usd < result.success_net_profit_usd


def test_simulate_atomic_bundle_missing_pricing_returns_none():
    opp = _opportunity(expected_profit_usd=None)
    assert simulate_atomic_bundle(opp, gas_cost_usd=1.0) is None


def test_simulate_atomic_bundle_no_legs_returns_none():
    opp = _opportunity(legs=[])
    assert simulate_atomic_bundle(opp, gas_cost_usd=1.0) is None


def test_as_atomic_opportunity_tags_the_atomic_strategy_and_a_fresh_id():
    opp = _opportunity()
    result = simulate_atomic_bundle(opp, gas_cost_usd=1.0)
    atomic_opp = as_atomic_opportunity(opp, result)
    assert atomic_opp.strategy == Strategy.ATOMIC
    assert atomic_opp.id != opp.id
    assert atomic_opp.expected_profit_usd == pytest.approx(result.expected_value_usd)


def test_as_atomic_opportunity_never_mutates_the_original():
    opp = _opportunity()
    original_profit = opp.expected_profit_usd
    original_strategy = opp.strategy
    result = simulate_atomic_bundle(opp, gas_cost_usd=1.0)
    as_atomic_opportunity(opp, result)
    assert opp.expected_profit_usd == original_profit
    assert opp.strategy == original_strategy
