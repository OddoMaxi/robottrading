import pytest

from app.simulation.portfolios import VirtualPortfolio


def make_portfolio(balance: float = 1_000.0) -> VirtualPortfolio:
    return VirtualPortfolio(name="1K", initial_capital_usd=balance, balances={"USDT": balance})


def test_available_usd_equals_balance_with_nothing_locked():
    portfolio = make_portfolio(1_000.0)
    assert portfolio.available_usd(now=0.0) == pytest.approx(1_000.0)


def test_locking_capital_reduces_available():
    portfolio = make_portfolio(1_000.0)
    assert portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0, now=0.0) is True
    assert portfolio.available_usd(now=50.0) == pytest.approx(600.0)


def test_multiple_locks_stack():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0, now=0.0)
    portfolio.lock_capital("basis:binance:ETH/USDT", 300.0, expiry=200.0, now=0.0)
    assert portfolio.available_usd(now=50.0) == pytest.approx(300.0)


def test_lock_expires_and_frees_capital():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0, now=0.0)
    assert portfolio.available_usd(now=99.0) == pytest.approx(600.0)
    assert portfolio.available_usd(now=101.0) == pytest.approx(1_000.0)


def test_reopening_the_same_key_replaces_the_old_lock():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0, now=0.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 700.0, expiry=200.0, now=50.0)
    assert portfolio.available_usd(now=50.0) == pytest.approx(300.0)  # not 400+700


def test_compound_mode_reference_capital_grows_with_balance():
    portfolio = VirtualPortfolio(name="1K", initial_capital_usd=1_000.0, balances={"USDT": 1_200.0}, capital_mode="compound")
    assert portfolio.reference_capital_usd == pytest.approx(1_200.0)


def test_fixed_mode_reference_capital_ignores_profit():
    portfolio = VirtualPortfolio(name="1K", initial_capital_usd=1_000.0, balances={"USDT": 1_200.0}, capital_mode="fixed")
    assert portfolio.reference_capital_usd == pytest.approx(1_000.0)


# --- Urgent audit fix: capital reservation must be atomic and never let
# available_capital go negative (spec section 1). ---


def test_lock_capital_rejects_a_reservation_larger_than_available():
    portfolio = make_portfolio(100.0)
    reserved = portfolio.lock_capital("basis:binance:BTC/USDT", 400.0, expiry=100.0, now=50.0)
    assert reserved is False
    assert portfolio.available_usd(now=50.0) == pytest.approx(100.0)  # nothing was reserved


def test_lock_capital_rejects_only_the_amount_that_would_overshoot():
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 700.0, expiry=100.0, now=0.0)
    # 700 locked, 300 free — a second, independent 400 request must be rejected.
    reserved = portfolio.lock_capital("basis:binance:ETH/USDT", 400.0, expiry=100.0, now=0.0)
    assert reserved is False
    assert portfolio.available_usd(now=0.0) == pytest.approx(300.0)


def test_available_usd_never_goes_negative_because_over_allocation_is_rejected():
    """The bug this guards against, found in production: a stale position
    sized under since-superseded risk rules got reconstructed on restart
    and would have pushed available_capital negative — this must be
    impossible by construction, not clamped after the fact."""
    portfolio = make_portfolio(500.0)
    assert portfolio.lock_capital("basis:binance:BTC/USDT", 5_000.0, expiry=100.0, now=0.0) is False
    assert portfolio.available_usd(now=0.0) == pytest.approx(500.0)


def test_reopening_the_same_key_excludes_its_own_prior_amount_from_the_check():
    """Re-locking a key you already hold shouldn't double-count that key's
    own existing reservation against itself."""
    portfolio = make_portfolio(1_000.0)
    portfolio.lock_capital("basis:binance:BTC/USDT", 900.0, expiry=100.0, now=0.0)
    # Re-opening the SAME key at a similar size must succeed (releases the
    # old 900 first) even though 900+900 would exceed the 1,000 balance.
    reserved = portfolio.lock_capital("basis:binance:BTC/USDT", 950.0, expiry=200.0, now=50.0)
    assert reserved is True
    assert portfolio.available_usd(now=50.0) == pytest.approx(50.0)
