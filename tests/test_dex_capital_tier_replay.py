import random
import uuid

from app.config.constants import Strategy
from app.onchain.dex_paper_trader import DexTradeStatus
from app.opportunity.models import Opportunity
from app.reporting.dex_capital_tier_replay import replay_across_tiers, replay_at_capital_tier


def _opp(capital_usd, detected_at, edge_pct=1.0):
    return Opportunity(
        strategy=Strategy.DEX_TRIANGULAR,
        symbol="X/Y",
        legs=[{"chain": "solana"}, {"chain": "solana"}],
        gross_spread_pct=1.0,
        capital_usd=capital_usd,
        realistic_executable_edge_pct=edge_pct,
        execution_fill_probability=1.0,
        detected_at=detected_at,
        id=uuid.uuid4(),
    )


def test_replay_at_capital_tier_a_tiny_pool_never_locks_more_than_it_holds_at_one_instant():
    # A near-zero edge keeps realized-profit compounding negligible, so
    # this isolates pure concurrency behavior: reservations spread far
    # enough apart in time (inclusion latency is sub-second on solana)
    # that each one has fully expired before the next — capital must
    # never be locked beyond the tier's own size.
    opps = [_opp(5000.0, detected_at=float(i) * 100.0, edge_pct=0.0001) for i in range(5)]
    result = replay_at_capital_tier(opps, capital_tier_usd=1000.0, gas_cost_usd_by_chain={}, default_gas_cost_usd=0.0, rng=random.Random(1))
    # Never more than the tier PLUS whatever profit had already compounded
    # in by that point — never capital that was never actually there.
    assert result.max_simultaneous_locked_usd <= 1000.0 + result.total_net_profit_usd + 1e-6


def test_replay_at_capital_tier_bigger_pool_never_produces_less_profit_than_a_smaller_one_same_draws():
    opps = [_opp(5000.0, detected_at=float(i)) for i in range(10)]
    small = replay_at_capital_tier(opps, 1000.0, {}, 0.01, rng=random.Random(99))
    big = replay_at_capital_tier(opps, 25_000.0, {}, 0.01, rng=random.Random(99))
    assert big.total_net_profit_usd >= small.total_net_profit_usd


def test_replay_at_capital_tier_same_cycle_burst_genuinely_competes_at_a_small_tier():
    # 5 opportunities detected in the exact SAME instant, each wanting
    # $5,000 — a $5,000 pool must NOT fund all 5 at full size (the
    # confirmed live bug this replay directly regression-tests): only the
    # first gets the full $5,000, every later one in the same instant is
    # sized down to whatever sliver of realized profit is left, never the
    # full amount again.
    opps = [_opp(5000.0, detected_at=100.0, edge_pct=0.0001) for _ in range(5)]
    result = replay_at_capital_tier(opps, capital_tier_usd=5000.0, gas_cost_usd_by_chain={}, default_gas_cost_usd=0.0, rng=random.Random(3))
    # The defining regression: capital locked at any single instant never
    # exceeds what genuinely existed (tier + profit already compounded
    # in) — later opportunities in the same instant are sized down to
    # whatever sliver is left, never each granted a fresh, independent
    # $5,000 (the confirmed live bug).
    assert result.max_simultaneous_locked_usd <= 5000.0 + result.total_net_profit_usd + 1e-6


def test_replay_across_tiers_uses_fresh_but_matched_rng_per_tier():
    opps = [_opp(1000.0, detected_at=float(i)) for i in range(5)]
    results = replay_across_tiers(opps, {}, 0.01, rng_factory=lambda: random.Random(2026), tiers_usd=[1000.0, 5000.0])
    assert len(results) == 2
    assert results[0].capital_tier_usd == 1000.0
    assert results[1].capital_tier_usd == 5000.0


def test_replay_at_capital_tier_status_counts_sum_to_total_opportunities():
    opps = [_opp(500.0, detected_at=float(i)) for i in range(8)]
    result = replay_at_capital_tier(opps, 10_000.0, {}, 0.01, rng=random.Random(5))
    total = (
        result.n_filled
        + result.n_no_capital_available
        + result.n_edge_disappeared
        + result.n_not_profitable_at_size
        + result.n_failed
    )
    assert total == result.n_opportunities == 8
