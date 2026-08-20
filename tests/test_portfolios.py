import pytest

from app.simulation.portfolios import VirtualPortfolio


def make_portfolio(balance: float = 1_000.0) -> VirtualPortfolio:
    return VirtualPortfolio(name="1K", initial_capital_usd=balance, balances={"USDT": balance})


def test_available_usd_equals_balance_with_nothing_locked():
    portfolio = make_portfolio(1_000.0)
    assert portfolio.available_usd(now=0.0) == pytest.approx(1_000.0)


def test_locking_capital_reduces_available():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0)
    assert portfolio.available_usd(now=50.0) == pytest.approx(600.0)


def test_multiple_locks_stack():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0)
    portfolio.lock_capital("basis:binance:ETH/USDT", 300.0, expiry=200.0)
    assert portfolio.available_usd(now=50.0) == pytest.approx(300.0)


def test_lock_expires_and_frees_capital():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0)
    assert portfolio.available_usd(now=99.0) == pytest.approx(600.0)
    assert portfolio.available_usd(now=101.0) == pytest.approx(1_000.0)


def test_available_usd_never_goes_negative():
    portfolio = make_portfolio(100.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0)  # locked more than the balance
    assert portfolio.available_usd(now=50.0) == pytest.approx(0.0)


def test_reopening_the_same_key_replaces_the_old_lock():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 700.0, expiry=200.0)
    assert portfolio.available_usd(now=50.0) == pytest.approx(300.0)  # not 400+700
