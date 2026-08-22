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
    resize_at_attempt,
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


def _dex_cross_opportunity_with_legs(**overrides) -> Opportunity:
    """A dex_cross opportunity whose legs carry the price/tvl_usd/fee_pct
    snapshot needed for resize_at_attempt's genuine re-optimization
    (same shape app.reporting.dex_replay relies on)."""
    legs = [
        {"chain": "solana", "exchange": "raydium", "pool_id": "pool_a", "price": 100.0, "tvl_usd": 2_000_000.0, "fee_pct": 0.25},
        {"chain": "solana", "exchange": "orca", "pool_id": "pool_b", "price": 101.0, "tvl_usd": 2_000_000.0, "fee_pct": 0.25},
    ]
    return _opportunity(legs=legs, **overrides)


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


# --- DexCapitalPool (reality audit, 2026-08-22: time-windowed reservations) ---


def test_capital_pool_locks_capital_until_release_at():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    assert pool.available_capital_usd(now=0.0) == 1000.0
    assert pool.reserve(uuid.uuid4(), 400.0, now=0.0, release_at=10.0) is True
    assert pool.available_capital_usd(now=0.0) == 600.0
    assert pool.available_capital_usd(now=9.99) == 600.0  # still locked, window hasn't elapsed
    assert pool.available_capital_usd(now=10.0) == 1000.0  # window elapsed, auto-freed


def test_capital_pool_never_over_reserves():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    assert pool.reserve(uuid.uuid4(), 1000.0, now=0.0, release_at=10.0) is True
    assert pool.reserve(uuid.uuid4(), 0.01, now=0.0, release_at=10.0) is False  # nothing left within the window
    assert pool.available_capital_usd(now=0.0) == 0.0


def test_capital_pool_resolve_pnl_books_immediately_but_principal_stays_locked():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    pool.reserve(uuid.uuid4(), 500.0, now=0.0, release_at=10.0)
    pool.resolve_pnl(12.5)
    assert pool.realized_pnl_usd == pytest.approx(12.5)
    assert pool.available_capital_usd(now=0.0) == pytest.approx(1000.0 + 12.5 - 500.0)  # profit booked, principal still locked
    assert pool.available_capital_usd(now=10.0) == pytest.approx(1000.0 + 12.5)  # principal freed after the window


def test_capital_pool_resolve_pnl_correctly_reduces_available_on_a_loss():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    pool.reserve(uuid.uuid4(), 500.0, now=0.0, release_at=10.0)
    pool.resolve_pnl(-3.0)
    assert pool.available_capital_usd(now=10.0) == pytest.approx(997.0)


def test_capital_pool_two_opportunities_in_the_same_cycle_genuinely_compete_for_capital():
    """Reality audit section 7's core finding: two opportunities detected
    in the same scan cycle (same `now`) must NOT each see the full pool —
    confirmed live as a bug (an atomic/dex_triangular sibling pair both
    reserved the full $5,000 pool for an overlapping window)."""
    pool = DexCapitalPool(total_capital_usd=5000.0)
    same_now = 100.0
    assert pool.reserve(uuid.uuid4(), 5000.0, now=same_now, release_at=same_now + 19.0) is True
    # A second, DIFFERENT opportunity attempted moments later in the SAME
    # cycle (same `now`) must see reduced capital, not a freshly-full pool.
    assert pool.available_capital_usd(now=same_now) == 0.0
    assert pool.reserve(uuid.uuid4(), 5000.0, now=same_now, release_at=same_now + 19.0) is False


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


# --- resize_at_attempt (reality audit section 4) ---


def test_resize_at_attempt_returns_none_for_non_dex_cross_strategy():
    opp = _opportunity(strategy=Strategy.DEX_MULTIHOP)
    assert resize_at_attempt(opp, drift_pct=0.0, available_capital_usd=1000.0, gas_cost_usd=0.01) is None


def test_resize_at_attempt_returns_none_when_legs_lack_price_snapshot():
    opp = _opportunity()  # legs have no price/tvl_usd/fee_pct
    assert resize_at_attempt(opp, drift_pct=0.0, available_capital_usd=1000.0, gas_cost_usd=0.01) is None


def test_resize_at_attempt_finds_a_profitable_size_capped_by_available_capital():
    opp = _dex_cross_opportunity_with_legs(capital_usd=500_000.0)  # detection-time size far beyond what's available now
    result = resize_at_attempt(opp, drift_pct=0.0, available_capital_usd=1000.0, gas_cost_usd=0.01)
    assert result is not None
    assert result.capital_usd <= 1000.0
    assert result.net_profit_usd > 0


def test_resize_at_attempt_negative_drift_can_wipe_out_profitability():
    opp = _dex_cross_opportunity_with_legs()
    # A large negative drift erases the 1% price gap entirely.
    result = resize_at_attempt(opp, drift_pct=-5.0, available_capital_usd=1000.0, gas_cost_usd=0.01)
    assert result is None


# --- attempt_dex_trade ---


def test_attempt_dex_trade_fills_and_locks_then_frees_capital_after_the_window():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity()
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.status == DexTradeStatus.FILLED
    assert result.capital_usd == 500.0
    assert result.net_profit_usd == pytest.approx(500.0 * 0.25 / 100)
    # Principal still locked immediately after (real inclusion window hasn't elapsed).
    assert pool.available_capital_usd(now=result.execution_attempt_timestamp) < 1000.0 + result.net_profit_usd
    # Fully freed once the real window has passed.
    assert pool.available_capital_usd(now=result.execution_complete_timestamp) == pytest.approx(1000.0 + result.net_profit_usd)


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
    assert pool.available_capital_usd(now=result.execution_complete_timestamp) == pytest.approx(1000.0 - 0.75)


def test_attempt_dex_trade_no_capital_available_when_pool_is_exhausted():
    pool = DexCapitalPool(total_capital_usd=100.0)
    pool.reserve(uuid.uuid4(), 100.0, now=0.0, release_at=1e12)  # nothing left, for a very long time
    opp = _opportunity(capital_usd=500.0)
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng(), now=0.0)
    assert result.status == DexTradeStatus.NO_CAPITAL_AVAILABLE
    assert result.capital_usd == 0.0
    assert result.net_profit_usd == 0.0


def test_attempt_dex_trade_sizes_down_to_whatever_capital_is_actually_available():
    pool = DexCapitalPool(total_capital_usd=200.0)  # less than the opportunity's own $500 optimal size
    opp = _opportunity(capital_usd=500.0)
    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng())
    assert result.status == DexTradeStatus.FILLED
    assert result.capital_usd == 200.0


def test_attempt_dex_trade_never_leaves_the_pool_with_lingering_locks_after_the_window():
    for rng in (_AlwaysFillRng(), _AlwaysFailRng()):
        pool = DexCapitalPool(total_capital_usd=1000.0)
        opp = _opportunity()
        result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=rng)
        assert pool.locked_capital_usd(now=result.execution_complete_timestamp) == 0.0


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
    assert pool.available_capital_usd(now=result.execution_complete_timestamp) == 1000.0  # nothing was ever reserved


def test_attempt_dex_trade_never_executes_when_revalidated_edge_is_negative():
    pool = DexCapitalPool(total_capital_usd=1000.0)
    opp = _opportunity(realistic_executable_edge_pct=0.01)

    class _BigNegativeDriftRng:
        def gauss(self, mu, sigma):
            return -10.0  # wipes out any realistic edge

        def random(self):
            return 0.0

    result = attempt_dex_trade(opp, pool, gas_cost_usd=0.002, rng=_BigNegativeDriftRng())
    assert result.status == DexTradeStatus.EDGE_DISAPPEARED
    assert result.capital_usd == 0.0
    assert pool.available_capital_usd(now=result.execution_complete_timestamp) == 1000.0  # never reserved


def test_attempt_dex_trade_two_opportunities_in_the_same_cycle_do_not_both_see_the_full_pool():
    """Direct regression for the reality audit's confirmed live bug: an
    atomic/dex_triangular sibling pair both reserved the full pool for an
    overlapping window because the old synchronous resolve() meant no
    real time separated reservation from release."""
    pool = DexCapitalPool(total_capital_usd=5000.0)
    scan_time = 1_000_000.0
    # A tiny (not zero) edge — keeps the trade "still valid" without the
    # booked profit itself materially reopening capital for the second
    # attempt, which would muddy what's being tested here.
    first = _opportunity(capital_usd=5000.0, realistic_executable_edge_pct=0.001, detected_at=scan_time)
    second = _opportunity(capital_usd=5000.0, realistic_executable_edge_pct=0.001, detected_at=scan_time)

    result_1 = attempt_dex_trade(first, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng(), now=scan_time)
    result_2 = attempt_dex_trade(second, pool, gas_cost_usd=0.002, rng=_AlwaysFillRng(), now=scan_time)

    assert result_1.status == DexTradeStatus.FILLED
    assert result_1.capital_usd == 5000.0
    # The second opportunity, attempted moments later in the SAME cycle,
    # must NOT see the full $5,000 pool again — the first trade's capital
    # is genuinely still locked in flight, not synchronously "back to
    # full" the instant the outcome was decided.
    assert result_2.capital_usd < 1.0
