import random

import pytest

from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, Strategy
from app.opportunity.models import Opportunity
from app.risk.limits import RiskLimits
from app.simulation.paper_trader import EMERGENCY_UNWIND_COST_PCT, EXECUTION_SLIPPAGE_MEAN_PCT, PaperTrader, TradeStatus
from app.simulation.portfolios import VirtualPortfolio


class _DeterministicRng:
    """Stub for tests that need exact profit assertions: `.random()` never
    trips a rare-event `< probability` check, `.gauss()` always returns
    exactly the mean (zero variance) instead of a real sample, `.uniform()`
    always returns its low bound (used for latency sampling)."""

    def random(self) -> float:
        return 1.0

    def gauss(self, mu: float, sigma: float) -> float:
        return mu

    def uniform(self, low: float, high: float) -> float:
        return low


class _AlwaysLegFailureRng:
    """Stub that always triggers the leg-failure/emergency-unwind branch."""

    def random(self) -> float:
        return 0.0

    def gauss(self, mu: float, sigma: float) -> float:
        return mu

    def uniform(self, low: float, high: float) -> float:
        return low


def make_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        strategy=Strategy.CROSS_EXCHANGE,
        symbol="BTC/USDT",
        legs=[],
        gross_spread_pct=0.5,
        net_spread_pct=0.3,
        capital_usd=DEFAULT_OPPORTUNITY_CAPITAL_USD,
        expected_profit_usd=3.0,
        execution_mode="taker_taker",
        execution_fill_probability=1.0,
        market_data_age_seconds=0.1,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def make_basis_opportunity(**overrides) -> Opportunity:
    defaults = dict(
        strategy=Strategy.BASIS,
        symbol="BTC/USDT",
        legs=[{"exchange": "binance", "side": "buy", "market": "spot"}],
        gross_spread_pct=0.5,
        net_spread_pct=0.3,
        capital_usd=DEFAULT_OPPORTUNITY_CAPITAL_USD,  # priced at $1,000
        expected_profit_usd=3.0,  # 0.3% of $1,000
        execution_mode=None,
        execution_fill_probability=None,
        market_data_age_seconds=0.1,
        holding_period_seconds=3600.0,
        capital_is_liquidity_capped=False,  # matches the real BasisArbitrageEngine — no depth data for the futures leg
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def test_execution_slippage_makes_net_profit_differ_from_the_priced_edge():
    """Urgent audit, item 5 — a real fill isn't guaranteed to capture
    exactly the detection-time priced edge. With a real (unstubbed) RNG,
    the executed profit should essentially never land on exactly the
    unadjusted priced value."""
    trader = PaperTrader(rng=random.Random(7))
    opp = make_opportunity(legs=[{"exchange": "binance", "side": "buy", "market": "spot"}])  # single leg — no unwind risk
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.net_profit_usd != pytest.approx(3.0)


def test_slippage_can_push_a_thin_margin_trade_negative_over_many_samples():
    """A trade must be able to lose money — not just occasionally break
    even. Runs enough independent samples that at least one going negative
    is a near-certainty if the risk modeling actually has bite."""
    opp = make_opportunity(legs=[], expected_profit_usd=0.5, gross_spread_pct=0.06, net_spread_pct=0.05)
    saw_a_loss = False
    for seed in range(200):
        trader = PaperTrader(rng=random.Random(seed))
        portfolio = make_portfolio(1_000.0)
        trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)
        if trade.net_profit_usd < 0:
            saw_a_loss = True
            break
    assert saw_a_loss


def test_leg_failure_triggers_emergency_unwind_at_a_loss():
    trader = PaperTrader(rng=_AlwaysLegFailureRng(), risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_basis_opportunity()  # 1-leg by default; give it 2 to make unwind risk apply
    opp.legs = [
        {"exchange": "binance", "side": "buy", "market": "spot"},
        {"exchange": "binance", "side": "sell", "market": "futures"},
    ]
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.status == TradeStatus.EMERGENCY_UNWIND
    assert trade.net_profit_usd == pytest.approx(-trade.capital_usd * (EMERGENCY_UNWIND_COST_PCT / 100))
    assert trade.net_profit_usd < 0


def test_single_leg_opportunity_is_never_at_risk_of_emergency_unwind():
    """Nothing to unwind with only one (or zero) legs — the risk is
    specifically about a multi-leg arbitrage's legs filling independently."""
    trader = PaperTrader(rng=_AlwaysLegFailureRng())
    opp = make_opportunity(legs=[{"exchange": "binance", "side": "buy", "market": "spot"}])
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.status != TradeStatus.EMERGENCY_UNWIND


def test_liquidity_capped_trade_never_scales_above_its_priced_capital():
    """Cross-Exchange/Triangular/Stablecoin capital_usd is a real VWAP fill —
    scaling it up for a big portfolio would pretend the book has more depth
    than what was actually observed."""
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_opportunity(capital_usd=800.0, holding_period_seconds=8.0, capital_is_liquidity_capped=True)
    portfolio = make_portfolio(25_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.capital_usd == pytest.approx(800.0)


def make_portfolio(balance: float = 1_000.0) -> VirtualPortfolio:
    return VirtualPortfolio(name="1K", initial_capital_usd=balance, balances={"USDT": balance})


def test_stale_data_gives_simulated_failed():
    trader = PaperTrader()
    opp = make_opportunity(market_data_age_seconds=10.0)
    assert trader.determine_outcome(opp) == TradeStatus.SIMULATED_FAILED


def test_partial_liquidity_gives_partial_fill():
    trader = PaperTrader()
    opp = make_opportunity(capital_usd=DEFAULT_OPPORTUNITY_CAPITAL_USD * 0.5)
    assert trader.determine_outcome(opp) == TradeStatus.PARTIAL_FILL


def test_comfortable_edge_survives_latency_revalidation():
    """Reality Engine spec, sections 10-11 — an opportunity with a real
    break_even_pct goes through latency revalidation; a comfortably
    profitable one should essentially always survive it."""
    trader = PaperTrader(rng=_DeterministicRng())
    opp = make_opportunity(net_spread_pct=0.30, break_even_pct=0.05)
    assert trader.determine_outcome(opp) == TradeStatus.SIMULATED_EXECUTED


def test_razor_thin_edge_can_be_missed_by_latency_revalidation():
    """A spread priced right at its break-even floor must sometimes fail
    revalidation over many samples — otherwise latency has no teeth.

    EDGE_DISAPPEARED, not MISSED (Opportunity Expansion spec, Step 5,
    2026-08-21) — this is the market moving/closing before the simulated
    execution moment, an economically different failure from a maker leg
    not filling, which is what MISSED is reserved for now."""
    saw_edge_disappeared = False
    for seed in range(500):
        trader = PaperTrader(rng=random.Random(seed))
        opp = make_opportunity(net_spread_pct=0.051, break_even_pct=0.05)
        if trader.determine_outcome(opp) == TradeStatus.EDGE_DISAPPEARED:
            saw_edge_disappeared = True
            break
    assert saw_edge_disappeared


def test_opportunity_without_break_even_pct_skips_latency_revalidation():
    """Basis/Funding never set break_even_pct — a few hundred ms is
    economically irrelevant against a multi-day carry position, so they
    should never be rejected by this check."""
    trader = PaperTrader(rng=_DeterministicRng())
    opp = make_opportunity(net_spread_pct=0.30, break_even_pct=None)
    assert trader.determine_outcome(opp) == TradeStatus.SIMULATED_EXECUTED


def test_taker_taker_never_misses_regardless_of_probability():
    trader = PaperTrader(rng=random.Random(0))
    opp = make_opportunity(execution_mode="taker_taker", execution_fill_probability=0.01)
    assert trader.determine_outcome(opp) == TradeStatus.SIMULATED_EXECUTED


def test_taker_taker_can_still_fail_latency_revalidation_but_never_as_missed():
    """Bug found live, 2026-08-21: 55 of 265 "missed" trades in a 24h window
    were TAKER_TAKER — supposed to be a guaranteed fill by construction
    (execution_fill_probability=1.0, the maker-fill check above explicitly
    skips taker_taker). The real cause was latency revalidation, mislabeled
    under the same MISSED status as a genuine maker-fill failure. A
    taker/taker order can still fail latency revalidation (the edge itself
    can close regardless of execution mode) — it must come back as
    EDGE_DISAPPEARED, never MISSED, so the two causes stay distinguishable."""
    saw_edge_disappeared = False
    for seed in range(500):
        trader = PaperTrader(rng=random.Random(seed))
        opp = make_opportunity(execution_mode="taker_taker", execution_fill_probability=1.0, net_spread_pct=0.051, break_even_pct=0.05)
        outcome = trader.determine_outcome(opp)
        assert outcome != TradeStatus.MISSED
        if outcome == TradeStatus.EDGE_DISAPPEARED:
            saw_edge_disappeared = True
    assert saw_edge_disappeared


def test_maker_mode_can_miss_when_roll_fails():
    # rng seeded so the first random() call is deterministic and known to be > 0.01
    trader = PaperTrader(rng=random.Random(1))
    opp = make_opportunity(execution_mode="maker_taker", execution_fill_probability=0.01)
    assert trader.determine_outcome(opp) == TradeStatus.MISSED


@pytest.mark.parametrize("status", [TradeStatus.MISSED, TradeStatus.EDGE_DISAPPEARED, TradeStatus.SIMULATED_FAILED])
def test_missed_and_failed_trades_cost_nothing(status):
    trader = PaperTrader()
    opp = make_opportunity()
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, status)

    assert trade.net_profit_usd == 0.0
    assert trade.capital_usd == 0.0
    assert portfolio.balances["USDT"] == pytest.approx(1_000.0)  # untouched


def test_executed_trade_books_profit_and_updates_balance():
    trader = PaperTrader(rng=_DeterministicRng())
    opp = make_opportunity()
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED)

    expected_profit = 3.0 + 1_000.0 * (EXECUTION_SLIPPAGE_MEAN_PCT / 100)
    assert trade.net_profit_usd == pytest.approx(expected_profit)
    assert portfolio.balances["USDT"] == pytest.approx(1_000.0 + expected_profit)


def test_outcome_is_shared_across_portfolios_for_the_same_opportunity():
    trader = PaperTrader(rng=random.Random(1))
    opp = make_opportunity(execution_mode="maker_taker", execution_fill_probability=0.01)
    outcome = trader.determine_outcome(opp)

    portfolio_a = make_portfolio(500.0)
    portfolio_b = make_portfolio(5_000.0)
    trade_a = trader.simulate(opp, portfolio_a, outcome)
    trade_b = trader.simulate(opp, portfolio_b, outcome)

    assert trade_a.status == trade_b.status == outcome


def test_hold_based_trade_scales_up_to_risk_limit_for_a_large_portfolio():
    trader = PaperTrader(rng=_DeterministicRng(), risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_basis_opportunity()  # priced at $1,000, 0.3% -> $3
    portfolio = make_portfolio(10_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.capital_usd == pytest.approx(5_000.0)  # capped by the risk limit, not the portfolio's full $10k
    expected_profit = 15.0 + 5_000.0 * (EXECUTION_SLIPPAGE_MEAN_PCT / 100)  # 5x the $1,000-priced profit, scaled linearly, minus slippage
    assert trade.net_profit_usd == pytest.approx(expected_profit)


def test_max_capital_per_trade_pct_scales_with_portfolio_size():
    """Spec section 31 — the default cap is 20% of the portfolio, not a flat dollar figure."""
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_pct=20.0, max_capital_per_trade_usd=5_000))
    small = make_portfolio(300.0)
    large = make_portfolio(10_000.0)

    trade_small = trader.simulate(make_basis_opportunity(), small, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)
    trade_large = trader.simulate(make_basis_opportunity(), large, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade_small.capital_usd == pytest.approx(60.0)  # 20% of $300
    assert trade_large.capital_usd == pytest.approx(2_000.0)  # 20% of $10,000, still under the $5k flat ceiling


def test_max_capital_per_trade_usd_still_caps_a_very_large_portfolio():
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_pct=20.0, max_capital_per_trade_usd=5_000))
    huge = make_portfolio(100_000.0)  # 20% would be $20k — the flat ceiling should win

    trade = trader.simulate(make_basis_opportunity(), huge, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.capital_usd == pytest.approx(5_000.0)


def test_hold_based_trade_respects_a_small_portfolio():
    trader = PaperTrader(rng=_DeterministicRng(), risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_basis_opportunity()
    portfolio = make_portfolio(300.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.capital_usd == pytest.approx(300.0)
    expected_profit = 0.9 + 300.0 * (EXECUTION_SLIPPAGE_MEAN_PCT / 100)  # 0.3% of $300, minus slippage
    assert trade.net_profit_usd == pytest.approx(expected_profit)


def test_hold_based_trade_locks_capital_on_the_portfolio():
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_basis_opportunity()
    portfolio = make_portfolio(1_000.0)

    trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert portfolio.available_usd(now=1_000.1) == pytest.approx(0.0)


def test_no_capital_available_when_already_fully_committed():
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    portfolio = make_portfolio(1_000.0)

    first = make_basis_opportunity(symbol="BTC/USDT")
    trader.simulate(first, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    second = make_basis_opportunity(symbol="ETH/USDT")  # a different position, same portfolio, no capital left
    trade = trader.simulate(second, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.1)

    assert trade.status == TradeStatus.NO_CAPITAL_AVAILABLE
    assert trade.capital_usd == 0.0


def test_max_concurrent_trades_blocks_a_new_position_even_with_capital_available():
    # Each trade is capped at $5,000 on a $50,000 portfolio — two open
    # positions leave $40,000 free, so a third being blocked can only be
    # the concurrency cap, not a capital shortfall.
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0, max_concurrent_trades=2))
    portfolio = make_portfolio(50_000.0)

    trader.simulate(make_basis_opportunity(symbol="BTC/USDT"), portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)
    trader.simulate(make_basis_opportunity(symbol="ETH/USDT"), portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.1)

    third = trader.simulate(make_basis_opportunity(symbol="SOL/USDT"), portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.2)

    assert third.status == TradeStatus.MAX_CONCURRENT_POSITIONS
    assert third.capital_usd == 0.0


def test_max_concurrent_trades_frees_up_once_a_position_closes():
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=100_000, max_capital_per_trade_pct=100.0, max_concurrent_trades=1))
    portfolio = make_portfolio(50_000.0)

    trader.simulate(make_basis_opportunity(symbol="BTC/USDT", holding_period_seconds=100.0), portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    blocked = trader.simulate(make_basis_opportunity(symbol="ETH/USDT"), portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_050.0)
    assert blocked.status == TradeStatus.MAX_CONCURRENT_POSITIONS

    after_close = trader.simulate(make_basis_opportunity(symbol="ETH/USDT"), portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_101.0)
    assert after_close.status == TradeStatus.SIMULATED_EXECUTED


def test_locked_capital_frees_up_after_the_holding_period_expires():
    trader = PaperTrader(rng=_DeterministicRng(), risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    portfolio = make_portfolio(1_000.0)
    opp = make_basis_opportunity(holding_period_seconds=3600.0)

    trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)
    assert portfolio.available_usd(now=1_000.0 + 3600.0 - 1) == pytest.approx(0.0)
    expected_balance = 1_000.0 + 3.0 + 1_000.0 * (EXECUTION_SLIPPAGE_MEAN_PCT / 100)  # principal + its booked profit, both freed
    assert portfolio.available_usd(now=1_000.0 + 3600.0 + 1) == pytest.approx(expected_balance)
