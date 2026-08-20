import random

import pytest

from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, Strategy
from app.opportunity.models import Opportunity
from app.risk.limits import RiskLimits
from app.simulation.paper_trader import PaperTrader, TradeStatus
from app.simulation.portfolios import VirtualPortfolio


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


def test_taker_taker_never_misses_regardless_of_probability():
    trader = PaperTrader(rng=random.Random(0))
    opp = make_opportunity(execution_mode="taker_taker", execution_fill_probability=0.01)
    assert trader.determine_outcome(opp) == TradeStatus.SIMULATED_EXECUTED


def test_maker_mode_can_miss_when_roll_fails():
    # rng seeded so the first random() call is deterministic and known to be > 0.01
    trader = PaperTrader(rng=random.Random(1))
    opp = make_opportunity(execution_mode="maker_taker", execution_fill_probability=0.01)
    assert trader.determine_outcome(opp) == TradeStatus.MISSED


def test_missed_and_failed_trades_cost_nothing():
    trader = PaperTrader()
    opp = make_opportunity()
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.MISSED)

    assert trade.net_profit_usd == 0.0
    assert trade.capital_usd == 0.0
    assert portfolio.balances["USDT"] == pytest.approx(1_000.0)  # untouched


def test_executed_trade_books_profit_and_updates_balance():
    trader = PaperTrader()
    opp = make_opportunity()
    portfolio = make_portfolio(1_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED)

    assert trade.net_profit_usd == pytest.approx(3.0)
    assert portfolio.balances["USDT"] == pytest.approx(1_003.0)


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
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_basis_opportunity()  # priced at $1,000, 0.3% -> $3
    portfolio = make_portfolio(10_000.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.capital_usd == pytest.approx(5_000.0)  # capped by the risk limit, not the portfolio's full $10k
    assert trade.net_profit_usd == pytest.approx(15.0)  # 5x the $1,000-priced profit, scaled linearly


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
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    opp = make_basis_opportunity()
    portfolio = make_portfolio(300.0)

    trade = trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)

    assert trade.capital_usd == pytest.approx(300.0)
    assert trade.net_profit_usd == pytest.approx(0.9)  # 0.3 * $300


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
    trader = PaperTrader(risk_limits=RiskLimits(max_capital_per_trade_usd=5_000, max_capital_per_trade_pct=100.0))
    portfolio = make_portfolio(1_000.0)
    opp = make_basis_opportunity(holding_period_seconds=3600.0)

    trader.simulate(opp, portfolio, TradeStatus.SIMULATED_EXECUTED, now=1_000.0)
    assert portfolio.available_usd(now=1_000.0 + 3600.0 - 1) == pytest.approx(0.0)
    assert portfolio.available_usd(now=1_000.0 + 3600.0 + 1) == pytest.approx(1_003.0)  # principal + its booked profit, both freed
