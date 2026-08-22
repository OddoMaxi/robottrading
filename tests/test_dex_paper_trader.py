import random
import time
import uuid
from dataclasses import replace

import pytest

from app.config.constants import Strategy
from app.onchain.dex_paper_trader import (
    DECISION_LATENCY_SECONDS,
    DexCapitalPool,
    DexTradeStatus,
    attempt_dex_trade,
    revalidate_edge,
)
from app.opportunity.models import Opportunity


def _opportunity(**overrides) -> Opportunity:
    now = time.time()
    defaults = dict(
        strategy=Strategy.DEX_CROSS,
        symbol="SOL/USDC",
        legs=[{"chain": "solana", "exchange": "raydium"}, {"chain": "solana", "exchange": "orca"}],
        gross_spread_pct=1.0,
        net_spread_pct=0.25,
        capital_usd=500.0,
        expected_profit_usd=1.25,
        execution_fill_probability=0.85,
        holding_period_seconds=20.0,
        realistic_executable_edge_pct=0.25,
        detected_at=now,
        id=uuid.uuid4(),
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


class _AlwaysFillRng:
    """gauss returns exactly the mean (no drift) and random() always
    beats any fill probability < 1.0 — deterministic FILLED outcome."""

    def gauss(self, mu, sigma):
        return mu

    def random(self):
        return 0.0


class _AlwaysFailRng:
    def gauss(self, mu, sigma):
        return mu

    def random(self):
        return 1.0  # always exceeds any fill_probability <= 1.0, forcing FAILED


# --- DexCapitalPool ---


def test_capital_pool_invariant_holds_after_reserve_and_release():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    assert pool.available_capital_usd == 1000.0
    assert pool.reserve(400.0) is True
    assert pool.available_capital_usd == 600.0
    pool.release_reservation(400.0)
    assert pool.available_capital_usd == 1000.0


def test_capital_pool_never_over_reserves():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    assert pool.reserve(1000.0) is True
    assert pool.reserve(0.01) is False  # nothing left
    assert pool.available_capital_usd == 0.0


def test_capital_pool_resolve_books_pnl_and_frees_capital_immediately():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    pool.reserve(500.0)
    pool.resolve(500.0, net_profit_usd=12.5)
    assert pool.available_capital_usd == pytest.approx(1012.5)
    assert pool.realized_pnl_usd == pytest.approx(12.5)


def test_capital_pool_resolve_correctly_reduces_available_on_a_loss():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    pool.reserve(500.0)
    pool.resolve(500.0, net_profit_usd=-3.0)
    assert pool.available_capital_usd == pytest.approx(997.0)


# --- revalidate_edge ---


def test_revalidate_edge_no_drift_survives_a_comfortable_edge():
    opp = _opportunity(realistic_executable_edge_pct=0.5)
    still_valid, revalidated_pct = revalidate_edge(opp, "solana", _AlwaysFillRng())
    assert still_valid is True
    assert revalidated_pct == pytest.approx(0.5)


def test_revalidate_edge_ethereum_has_more_drift_variance_than_solana():
    """Real, chain-specific latency (app.onchain.execution_model) drives
    the drift's standard deviation — Ethereum's much longer inclusion
    window means more accumulated uncertainty, not an arbitrary difference."""
    opp = _opportunity(realistic_executable_edge_pct=0.1)
    rng = random.Random(123)
    eth_drifts = [revalidate_edge(opp, "eth", rng)[1] - 0.1 for _ in range(500)]
    rng2 = random.Random(123)
    sol_drifts = [revalidate_edge(opp, "solana", rng2)[1] - 0.1 for _ in range(500)]
    eth_variance = sum(d**2 for d in eth_drifts) / len(eth_drifts)
    sol_variance = sum(d**2 for d in sol_drifts) / len(sol_drifts)
    assert eth_variance > sol_variance


# --- attempt_dex_trade ---


def test_attempt_dex_trade_fills_and_reserves_then_frees_capital():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity()
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.status == DexTradeStatus.FILLED
    assert result.capital_usd == 500.0
    assert result.net_profit_usd == pytest.approx(500.0 * 0.25 / 100)
    assert pool.available_capital_usd == pytest.approx(1000.0 + result.net_profit_usd)  # fully freed + profit booked


def test_attempt_dex_trade_gas_is_not_double_counted_on_a_fill():
    """Regression: realistic_executable_edge_pct already has gas netted
    out at detection time (app.onchain.cross_dex_arbitrage/multihop_arbitrage
    both subtract gas before producing that %) — a FILLED trade must not
    subtract gas_cost_usd a second time."""
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity(capital_usd=1000.0, realistic_executable_edge_pct=1.0)
    result = attempt_dex_trade(opp, pool, gas_cost_usd=50.0, rng=_AlwaysFillRng())  # deliberately huge gas figure
    assert result.status == DexTradeStatus.FILLED
    assert result.net_profit_usd == pytest.approx(10.0)  # exactly 1% of $1000, gas untouched


def test_attempt_dex_trade_failed_roll_costs_only_gas():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity()
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.75, rng=_AlwaysFailRng())
    assert result.status == DexTradeStatus.FAILED
    assert result.net_profit_usd == pytest.approx(-0.75)
    assert pool.available_capital_usd == pytest.approx(1000.0 - 0.75)


def test_attempt_dex_trade_no_capital_available_when_pool_is_exhausted():
    pool = DexCapitalPool(total_capital_usd=100.0)
    pool.reserve(100.0)  # nothing left
    opp = _opportunity(capital_usd=500.0)
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.status == DexTradeStatus.NO_CAPITAL_AVAILABLE
    assert result.capital_usd == 0.0
    assert result.net_profit_usd == 0.0


def test_attempt_dex_trade_sizes_down_to_whatever_capital_is_actually_available():
    pool = DexCapitalPool(total_capital_usd=200.0)  # less than the opportunity's own $500 optimal size
    opp = _opportunity(capital_usd=500.0)
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.status == DexTradeStatus.FILLED
    assert result.capital_usd == 200.0


def test_attempt_dex_trade_never_leaves_the_pool_inconsistent_across_every_outcome():
    for rng in (_AlwaysFillRng(), _AlwaysFailRng()):
        pool = DexCapitalPool(total_capital_usd=1000.0)
        opp = _opportunity()
        attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=rng)
        assert pool.reserved_capital_usd == 0.0
        assert pool.deployed_capital_usd == 0.0


def test_attempt_dex_trade_timestamps_are_monotonic_and_reflect_real_chain_latency():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity()  # solana
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.detection_timestamp <= result.validation_timestamp <= result.execution_attempt_timestamp <= result.execution_complete_timestamp
    assert result.detection_to_validation_ms == pytest.approx(DECISION_LATENCY_SECONDS * 1000)
    assert result.validation_to_execution_ms > 0  # real inclusion latency, not instant


def test_attempt_dex_trade_ethereum_has_longer_execution_latency_than_solana():
    pool_a = DexCapitalPool(total_capital_usd=1000.0)
    pool_b = DexCapitalPool(total_capital_usd=1000.0)
    sol_opp = _opportunity()
    eth_opp = _opportunity(legs=[{"chain": "eth", "exchange": "uniswap_v3"}, {"chain": "eth", "exchange": "sushiswap"}])
    sol_result = attempt_dex_trade(sol_opp, pool_a, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    eth_result = attempt_dex_trade(eth_opp, pool_b, gas_cost_usd=5.0, rng=_AlwaysFillRng())
    assert eth_result.validation_to_execution_ms > sol_result.validation_to_execution_ms


def test_attempt_dex_trade_missing_chain_fails_gracefully():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity(legs=[])
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.status == DexTradeStatus.FAILED
    assert pool.available_capital_usd == 1000.0  # nothing was ever reserved
