import pytest

from app.reporting.real_net_worth import (
    check_reconciliation_invariant,
    compute_liquidation_net_worth,
    compute_real_wealth_pnl,
    compute_real_wealth_return_pct,
)


def test_liquidation_net_worth_sums_usdt_plus_priced_assets():
    balances = {"USDT": 30.0, "RVN": 6407.427, "ZIL": 8810.1673}
    prices = {"RVN": 0.0033, "ZIL": 0.002799}
    expected = 30.0 + 6407.427 * 0.0033 + 8810.1673 * 0.002799
    assert compute_liquidation_net_worth(balances, prices) == pytest.approx(expected)


def test_liquidation_net_worth_zero_haircut_by_default():
    balances = {"USDT": 0.0, "RVN": 1000.0}
    prices = {"RVN": 0.0033}
    assert compute_liquidation_net_worth(balances, prices) == pytest.approx(1000.0 * 0.0033)


def test_liquidation_net_worth_applies_disclosed_haircut_when_given():
    balances = {"RVN": 1000.0}
    prices = {"RVN": 0.0033}
    result = compute_liquidation_net_worth(balances, prices, liquidation_fee_rate=0.001)
    assert result == pytest.approx(1000.0 * 0.0033 * 0.999)


def test_liquidation_net_worth_skips_assets_with_no_known_price():
    balances = {"USDT": 10.0, "UNPRICED": 500.0}
    prices = {}
    assert compute_liquidation_net_worth(balances, prices) == pytest.approx(10.0)


def test_liquidation_net_worth_ignores_zero_and_negative_balances():
    balances = {"USDT": 10.0, "RVN": 0.0}
    prices = {"RVN": 0.0033}
    assert compute_liquidation_net_worth(balances, prices) == pytest.approx(10.0)


def test_real_wealth_pnl_is_current_minus_starting():
    assert compute_real_wealth_pnl(166.563440, 159.428213) == pytest.approx(-7.135227, abs=1e-6)


def test_real_wealth_return_pct():
    assert compute_real_wealth_return_pct(100.0, 95.0) == pytest.approx(-5.0)


def test_real_wealth_return_pct_none_for_nonpositive_starting_worth():
    assert compute_real_wealth_return_pct(0.0, 50.0) is None
    assert compute_real_wealth_return_pct(-10.0, 50.0) is None


def test_reconciliation_invariant_passes_within_tolerance():
    check = check_reconciliation_invariant(accounting_pnl_usd=24.021390, real_wealth_change_usd=24.021395, tolerance_usd=0.01)
    assert check.within_tolerance is True
    assert check.gap_usd == pytest.approx(-0.000005, abs=1e-6)


def test_reconciliation_invariant_the_v4_forensic_case_fails_by_a_wide_margin():
    """The exact real gap discovered in the V4 forensic reconstruction --
    this is precisely the case check_reconciliation_invariant exists to
    catch automatically, instead of requiring a manual forensic audit."""
    check = check_reconciliation_invariant(accounting_pnl_usd=24.021409, real_wealth_change_usd=-7.135227, tolerance_usd=0.01)
    assert check.within_tolerance is False
    assert check.gap_usd == pytest.approx(31.156636, abs=1e-6)


def test_reconciliation_invariant_the_v5_forensic_replay_case_passes():
    """The new engine's own replay of the same 606 real V4 fills closed
    to $0.000000 against the real wealth change -- the invariant must
    pass on that reconciled pair."""
    check = check_reconciliation_invariant(accounting_pnl_usd=-7.135227, real_wealth_change_usd=-7.135227, tolerance_usd=0.01)
    assert check.within_tolerance is True
    assert check.gap_usd == pytest.approx(0.0, abs=1e-9)
