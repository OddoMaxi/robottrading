import pytest

from app.execution.reconciliation import reconcile_base_asset_balance

# ---- Test 4 (user directive, 2026-08-24): exact SAND incident replay ------
#
# Binance inventory constituted: 237.0 gross, 236.763 net (after FIX 1's
# now-correct 0.237 SAND fee resolution). Arbitrage sold 232.0 SAND on
# Binance. Expected residual: 4.763 SAND -- and this must come back as a
# clean MATCH, not the "232 SAND deficit" the original flawed reconcile
# check (which never added the inventory constitution's own contribution
# into the expected side) reported.


def test_sand_incident_exact_replay_binance_side_matches():
    result = reconcile_base_asset_balance(
        exchange="binance",
        before_balance=0.0,
        after_balance=4.763,
        inventory_constitution_exchange="binance",
        inventory_constitution_net_qty=236.763,
        arbitrage_sell_exchange="binance",
        arbitrage_sell_qty=232.0,
    )
    assert result.match is True
    assert result.expected_delta == pytest.approx(4.763)
    assert result.difference == pytest.approx(0.0, abs=1e-9)


def test_sand_incident_would_have_falsely_mismatched_with_the_old_237_gross_figure():
    """Documents exactly what the original bug looked like: using the
    UNFIXED gross figure (237.0, ignoring the real 0.237 SAND fee) as
    if it were the net inventory produces a wrong expected residual of
    5.0, not the real 4.763 -- a small but genuine (and, before this
    fix, unexplained) 0.237 SAND gap would show up as a difference."""
    result = reconcile_base_asset_balance(
        exchange="binance",
        before_balance=0.0,
        after_balance=4.763,
        inventory_constitution_exchange="binance",
        inventory_constitution_net_qty=237.0,  # the pre-FIX-1 (wrong) figure
        arbitrage_sell_exchange="binance",
        arbitrage_sell_qty=232.0,
    )
    assert result.expected_delta == pytest.approx(5.0)
    assert result.difference == pytest.approx(-0.237, abs=1e-9)


# ---- Test 6 (user directive, 2026-08-24): reconciliation with Bybit
# residual inventory ---------------------------------------------------------
#
# The buy leg of the SAND arbitrage: bought net 232.767 SAND on Bybit,
# nothing sold there (the sell happened on Binance against pre-existing
# inventory) -- Bybit must retain the full net buy amount.


def test_reconciliation_with_bybit_residual_inventory_after_buy_leg():
    result = reconcile_base_asset_balance(
        exchange="bybit",
        before_balance=0.0,
        after_balance=232.767,
        arbitrage_buy_exchange="bybit",
        arbitrage_buy_net_qty=232.767,
    )
    assert result.match is True
    assert result.expected_delta == pytest.approx(232.767)


# ---- Test 5 (user directive, 2026-08-24): dust residual below exchange
# minimum -- a failed neutralization must not create a false mismatch ------
#
# execute_one_arbitrage's own residual-dust cleanup tried to neutralize
# 0.767 SAND on Bybit (232.767 bought - 232.0 matched by the sell) and
# Bybit rejected it (below its own minimum order value) -- the residual
# stays on Bybit. neutralization_qty=0 (nothing was actually filled) is
# exactly what a failed neutralization means, and reconciliation must
# still MATCH: the residual is already accounted for by simply never
# being subtracted.


def test_dust_residual_below_exchange_minimum_still_reconciles():
    result = reconcile_base_asset_balance(
        exchange="bybit",
        before_balance=0.0,
        after_balance=232.767,  # the FULL net buy amount -- the 0.767 residual was never removed
        arbitrage_buy_exchange="bybit",
        arbitrage_buy_net_qty=232.767,
        neutralization_exchange="bybit",
        neutralization_qty=0.0,  # the neutralization attempt failed -- zero actually filled
    )
    assert result.match is True


def test_a_successful_neutralization_is_subtracted():
    result = reconcile_base_asset_balance(
        exchange="bybit",
        before_balance=0.0,
        after_balance=232.0,  # the 0.767 residual WAS successfully sold off this time
        arbitrage_buy_exchange="bybit",
        arbitrage_buy_net_qty=232.767,
        neutralization_exchange="bybit",
        neutralization_qty=0.767,
    )
    assert result.match is True
    assert result.expected_delta == pytest.approx(232.0)


# ---- Test 3 (user directive, 2026-08-24): inventory constitution +
# later partial inventory consumption ----------------------------------------
#
# A batch that constitutes MORE inventory than a single arbitrage cycle
# consumes (the general case the SAND replay is one instance of) --
# residual inventory must reconcile cleanly regardless of the exact sizes.


def test_inventory_constitution_with_later_partial_consumption():
    result = reconcile_base_asset_balance(
        exchange="binance",
        before_balance=10.0,  # some pre-existing dust from an earlier cycle
        after_balance=10.0 + 500.0 - 300.0,
        inventory_constitution_exchange="binance",
        inventory_constitution_net_qty=500.0,
        arbitrage_sell_exchange="binance",
        arbitrage_sell_qty=300.0,
    )
    assert result.match is True
    assert result.expected_delta == pytest.approx(200.0)


def test_partial_consumption_leaves_correct_residual_for_a_future_cycle():
    result = reconcile_base_asset_balance(
        exchange="binance", before_balance=0.0, after_balance=200.0,
        inventory_constitution_exchange="binance", inventory_constitution_net_qty=500.0,
        arbitrage_sell_exchange="binance", arbitrage_sell_qty=300.0,
    )
    assert result.match is True
    # This residual (200.0) is exactly what a NEXT cycle's own
    # before_balance should read as pre-existing inventory -- the
    # identity composes across consecutive cycles.


# ---- FIX 3 (user directive, 2026-08-25): exact RVN rebalance-mismatch
# incident replay -------------------------------------------------------------
#
# Scan 2 of the first CONTINUOUS LIVE V2 session: REBALANCE_FIRST sold
# 2192.5 RVN on Binance (order 1344692249) to fund the upcoming arbitrage
# buy, which then bought back net 2125.1727 RVN there (order 1344692270).
# Before FIX 3, reconcile_base_asset_balance had no rebalance_sell_*
# parameter, so it expected only +2125.1727 and flagged a false -2192.5
# "BALANCE / LEDGER MISMATCH" that halted the session. Verified
# independently against real Binance myTrades before this fix was written.


def test_rvn_rebalance_mismatch_incident_exact_replay_now_reconciles():
    result = reconcile_base_asset_balance(
        exchange="binance",
        before_balance=14400.3175,
        after_balance=14332.9902,
        arbitrage_buy_exchange="binance",
        arbitrage_buy_net_qty=2125.1727,
        rebalance_sell_exchange="binance",
        rebalance_sell_qty=2192.5,
    )
    assert result.match is True
    assert result.expected_delta == pytest.approx(-67.3273, abs=1e-4)


def test_without_the_rebalance_term_the_incident_reproduces_the_false_mismatch():
    """Documents exactly what the original bug looked like: omitting the
    rebalance-sell contribution (the pre-FIX-3 call shape) reproduces the
    real observed -2192.5 false mismatch."""
    result = reconcile_base_asset_balance(
        exchange="binance",
        before_balance=14400.3175,
        after_balance=14332.9902,
        arbitrage_buy_exchange="binance",
        arbitrage_buy_net_qty=2125.1727,
    )
    assert result.match is False
    assert result.difference == pytest.approx(-2192.5, abs=1e-3)


def test_rebalance_sell_on_a_different_exchange_does_not_contribute():
    result = reconcile_base_asset_balance(
        exchange="bybit", before_balance=10.0, after_balance=10.0,
        rebalance_sell_exchange="binance", rebalance_sell_qty=2192.5,
    )
    assert result.expected_delta == 0.0
    assert result.match is True


def test_rebalance_sell_combines_with_inventory_and_arbitrage_legs():
    """A cycle that recycled inventory, rebalanced, AND arbitraged -- all
    on the same exchange -- must still reconcile via one combined
    identity (the general case FIX 3 exists to cover, not just the exact
    incident replay above)."""
    result = reconcile_base_asset_balance(
        exchange="binance", before_balance=100.0, after_balance=100.0 + 50.0 - 20.0 + 30.0,
        inventory_constitution_exchange="binance", inventory_constitution_net_qty=50.0,
        rebalance_sell_exchange="binance", rebalance_sell_qty=20.0,
        arbitrage_buy_exchange="binance", arbitrage_buy_net_qty=30.0,
    )
    assert result.match is True
    assert result.expected_delta == pytest.approx(60.0)


# ---- structural / negative cases -------------------------------------------


def test_genuine_mismatch_is_reported_as_such():
    result = reconcile_base_asset_balance(
        exchange="binance", before_balance=0.0, after_balance=1.0,  # 4 units missing, unexplained
        inventory_constitution_exchange="binance", inventory_constitution_net_qty=236.763,
        arbitrage_sell_exchange="binance", arbitrage_sell_qty=232.0,
    )
    assert result.match is False
    assert result.difference < -3.0


def test_fills_on_a_different_exchange_do_not_contribute():
    """A leg that happened on the OTHER exchange must never leak into
    this exchange's expected delta."""
    result = reconcile_base_asset_balance(
        exchange="binance", before_balance=0.0, after_balance=0.0,
        inventory_constitution_exchange="bybit", inventory_constitution_net_qty=500.0,
        arbitrage_buy_exchange="bybit", arbitrage_buy_net_qty=232.767,
    )
    assert result.expected_delta == 0.0
    assert result.match is True


def test_no_fills_anywhere_expects_zero_delta():
    result = reconcile_base_asset_balance(exchange="binance", before_balance=5.0, after_balance=5.0)
    assert result.expected_delta == 0.0
    assert result.match is True


def test_tolerance_absorbs_small_dust_but_not_a_real_gap():
    small = reconcile_base_asset_balance(exchange="binance", before_balance=0.0, after_balance=0.03, tolerance_abs=0.05)
    assert small.match is True  # within the documented dust tolerance
    real_gap = reconcile_base_asset_balance(exchange="binance", before_balance=0.0, after_balance=0.5, tolerance_abs=0.05)
    assert real_gap.match is False
