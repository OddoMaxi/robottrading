import random

import pytest

from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, Strategy
from app.opportunity.models import Opportunity
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
