import pytest

from app.execution.reconciliation import LedgerEvent, LedgerEventType, reconcile_asset_balance


def _event(event_type, exchange, base_asset, net_base_delta, *, gross_base_delta=None, side="BUY", quote_asset="USDT",
           quote_delta=None, fee_asset=None, fee_amount=None, order_id=None, timestamp=None) -> LedgerEvent:
    return LedgerEvent(
        event_type=event_type, exchange=exchange, base_asset=base_asset, quote_asset=quote_asset, side=side,
        gross_base_delta=gross_base_delta if gross_base_delta is not None else net_base_delta,
        net_base_delta=net_base_delta, quote_delta=quote_delta, fee_asset=fee_asset, fee_amount=fee_amount,
        order_id=order_id, timestamp=timestamp,
    )


# ---- Test 4 (user directive, 2026-08-24): exact SAND incident replay ------
#
# Binance inventory constituted: 237.0 gross, 236.763 net (after FIX 1's
# now-correct 0.237 SAND fee resolution). Arbitrage sold 232.0 SAND on
# Binance. Expected residual: 4.763 SAND -- and this must come back as a
# clean MATCH, not the "232 SAND deficit" the original flawed reconcile
# check (which never added the inventory constitution's own contribution
# into the expected side) reported.


def test_sand_incident_exact_replay_binance_side_matches():
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "SAND", 236.763),
        _event(LedgerEventType.ARBITRAGE_SELL, "binance", "SAND", -232.0, side="SELL"),
    )
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=4.763, events=events)
    assert result.match is True
    assert result.expected_delta == pytest.approx(4.763)
    assert result.difference == pytest.approx(0.0, abs=1e-9)


def test_sand_incident_would_have_falsely_mismatched_with_the_old_237_gross_figure():
    """Documents exactly what the original (pre-FIX-1) bug looked like:
    using the UNFIXED gross figure (237.0, ignoring the real 0.237 SAND
    fee) as if it were the net inventory produces a wrong expected
    residual of 5.0, not the real 4.763."""
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "SAND", 237.0),  # the pre-FIX-1 (wrong) figure
        _event(LedgerEventType.ARBITRAGE_SELL, "binance", "SAND", -232.0, side="SELL"),
    )
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=4.763, events=events)
    assert result.expected_delta == pytest.approx(5.0)
    assert result.difference == pytest.approx(-0.237, abs=1e-9)


# ---- Test 6 (user directive, 2026-08-24): reconciliation with Bybit
# residual inventory ---------------------------------------------------------


def test_reconciliation_with_bybit_residual_inventory_after_buy_leg():
    events = (_event(LedgerEventType.ARBITRAGE_BUY, "bybit", "SAND", 232.767),)
    result = reconcile_asset_balance(exchange="bybit", asset="SAND", before_balance=0.0, after_balance=232.767, events=events)
    assert result.match is True
    assert result.expected_delta == pytest.approx(232.767)


# ---- Test 5 (user directive, 2026-08-24): dust residual below exchange
# minimum -- a failed neutralization must not create a false mismatch ------


def test_dust_residual_below_exchange_minimum_still_reconciles():
    events = (
        _event(LedgerEventType.ARBITRAGE_BUY, "bybit", "SAND", 232.767),
        _event(LedgerEventType.NEUTRALIZATION, "bybit", "SAND", 0.0, side="SELL"),  # failed -- zero actually filled
    )
    result = reconcile_asset_balance(exchange="bybit", asset="SAND", before_balance=0.0, after_balance=232.767, events=events)
    assert result.match is True


def test_a_successful_neutralization_is_subtracted():
    events = (
        _event(LedgerEventType.ARBITRAGE_BUY, "bybit", "SAND", 232.767),
        _event(LedgerEventType.NEUTRALIZATION, "bybit", "SAND", -0.767, side="SELL"),
    )
    result = reconcile_asset_balance(exchange="bybit", asset="SAND", before_balance=0.0, after_balance=232.0, events=events)
    assert result.match is True
    assert result.expected_delta == pytest.approx(232.0)


# ---- Test 3 (user directive, 2026-08-24): inventory constitution +
# later partial inventory consumption ----------------------------------------


def test_inventory_constitution_with_later_partial_consumption():
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "SAND", 500.0),
        _event(LedgerEventType.ARBITRAGE_SELL, "binance", "SAND", -300.0, side="SELL"),
    )
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=10.0, after_balance=10.0 + 500.0 - 300.0, events=events)
    assert result.match is True
    assert result.expected_delta == pytest.approx(200.0)


def test_partial_consumption_leaves_correct_residual_for_a_future_cycle():
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "SAND", 500.0),
        _event(LedgerEventType.ARBITRAGE_SELL, "binance", "SAND", -300.0, side="SELL"),
    )
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=200.0, events=events)
    assert result.match is True
    # This residual (200.0) is exactly what a NEXT cycle's own
    # before_balance should read as pre-existing inventory -- the
    # identity composes across consecutive cycles.


# ---- structural / negative cases -------------------------------------------


def test_genuine_mismatch_is_reported_as_such():
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "SAND", 236.763),
        _event(LedgerEventType.ARBITRAGE_SELL, "binance", "SAND", -232.0, side="SELL"),
    )
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=1.0, events=events)  # 4 units missing, unexplained
    assert result.match is False
    assert result.difference < -3.0


def test_fills_on_a_different_exchange_do_not_contribute():
    """A leg that happened on the OTHER exchange must never leak into
    this exchange's expected delta."""
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "bybit", "SAND", 500.0),
        _event(LedgerEventType.ARBITRAGE_BUY, "bybit", "SAND", 232.767),
    )
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=0.0, events=events)
    assert result.expected_delta == 0.0
    assert result.match is True


def test_no_fills_anywhere_expects_zero_delta():
    result = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=5.0, after_balance=5.0, events=())
    assert result.expected_delta == 0.0
    assert result.match is True


def test_tolerance_absorbs_small_dust_but_not_a_real_gap():
    small = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=0.03, events=(), tolerance_abs=0.05)
    assert small.match is True  # within the documented dust tolerance
    real_gap = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=0.0, after_balance=0.5, events=(), tolerance_abs=0.05)
    assert real_gap.match is False


# ---- FIX 3 (user directive, 2026-08-25): exact RVN rebalance-mismatch
# incident replay -------------------------------------------------------------
#
# Scan 2 of the first CONTINUOUS LIVE V2 session: REBALANCE_FIRST sold
# 2192.5 RVN on Binance (order 1344692249) to fund the upcoming arbitrage
# buy, which then bought back net 2125.1727 RVN there (order 1344692270)
# -- BOTH events are for the SAME asset, RVN. Before FIX 3, the old
# reconcile function had no rebalance parameter at all and flagged a
# false -2192.5 mismatch.


def test_rvn_rebalance_mismatch_incident_exact_replay_now_reconciles():
    events = (
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "RVN", 2125.1727, order_id="1344692270"),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2192.5, side="SELL", order_id="1344692249"),
    )
    result = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=14400.3175, after_balance=14332.9902, events=events)
    assert result.match is True
    assert result.expected_delta == pytest.approx(-67.3273, abs=1e-4)


def test_without_the_rebalance_event_the_incident_reproduces_the_false_mismatch():
    """Documents exactly what the pre-FIX-3 bug looked like: omitting
    the rebalance-sell event reproduces the real observed -2192.5 false
    mismatch."""
    events = (_event(LedgerEventType.ARBITRAGE_BUY, "binance", "RVN", 2125.1727),)
    result = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=14400.3175, after_balance=14332.9902, events=events)
    assert result.match is False
    assert result.difference == pytest.approx(-2192.5, abs=1e-3)


def test_rebalance_sell_on_a_different_exchange_does_not_contribute():
    events = (_event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2192.5, side="SELL"),)
    result = reconcile_asset_balance(exchange="bybit", asset="RVN", before_balance=10.0, after_balance=10.0, events=events)
    assert result.expected_delta == 0.0
    assert result.match is True


def test_rebalance_sell_combines_with_inventory_and_arbitrage_legs_of_the_same_asset():
    """A cycle that recycled inventory, rebalanced, AND arbitraged -- all
    on the same exchange, ALL for the same asset -- must still reconcile
    via one combined identity."""
    events = (
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "RVN", 50.0),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -20.0, side="SELL"),
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "RVN", 30.0),
    )
    result = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=100.0, after_balance=100.0 + 50.0 - 20.0 + 30.0, events=events)
    assert result.match is True
    assert result.expected_delta == pytest.approx(60.0)


# ---- FIX 4 (user directive, 2026-08-25): MULTI-ASSET RECONCILIATION --------
#
# Item 3: exact live incident replay. The real first CONTINUOUS LIVE V3
# incident: REBALANCE_FIRST sold 2107.9 RVN on Binance to fund a ZIL
# arbitrage buy (net 2626.2711 ZIL). The old (FIX-3-shaped) reconcile
# function keyed the rebalance by exchange only and subtracted the RVN
# quantity from the ZIL check -- a false 2107.9-unit mismatch. FIX 4
# must reconcile ZIL cleanly, with the RVN rebalance event contributing
# EXACTLY ZERO to the ZIL calculation.


def test_zil_rvn_live_incident_exact_replay_reconciles_cleanly():
    events = (
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2107.9, side="SELL"),
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "ZIL", 2626.2711),
    )
    result = reconcile_asset_balance(exchange="binance", asset="ZIL", before_balance=6202.0917, after_balance=8828.3628, events=events)
    assert result.expected_delta == pytest.approx(2626.2711)
    assert result.actual_delta == pytest.approx(2626.2711)
    assert result.match is True

    rvn_events_included = [e for e in result.contributing_events if e.base_asset == "RVN"]
    assert rvn_events_included == []  # RVN rebalance quantity included in ZIL reconciliation = 0


def test_zil_rvn_live_incident_without_the_fix_would_have_mismatched():
    """Sanity check on the fixture itself: if a caller mistakenly builds
    events with the wrong base_asset (reproducing the pre-FIX-4 bug at
    the CALL SITE, not in this module), the mismatch reappears -- proving
    this replay genuinely exercises the real incident's shape."""
    mistagged_events = (
        _event(LedgerEventType.REBALANCE_SELL, "binance", "ZIL", -2107.9, side="SELL"),  # wrong: really RVN
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "ZIL", 2626.2711),
    )
    result = reconcile_asset_balance(exchange="binance", asset="ZIL", before_balance=6202.0917, after_balance=8828.3628, events=mistagged_events)
    assert result.match is False
    assert result.difference == pytest.approx(2107.9, abs=1e-3)


# Item 4: generic cross-asset contamination test. Rebalance asset A,
# arbitrage asset B, inventory constitution asset C, neutralization asset
# D -- each must affect only its own asset.


def test_cross_asset_contamination_generic():
    events = (
        _event(LedgerEventType.REBALANCE_SELL, "binance", "A", -10.0, side="SELL"),
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "B", 20.0),
        _event(LedgerEventType.INVENTORY_CONSTITUTION, "binance", "C", 30.0),
        _event(LedgerEventType.NEUTRALIZATION, "binance", "D", -5.0, side="SELL"),
    )
    balances = {"A": (100.0, 90.0), "B": (0.0, 20.0), "C": (0.0, 30.0), "D": (50.0, 45.0)}
    for asset, (before, after) in balances.items():
        result = reconcile_asset_balance(exchange="binance", asset=asset, before_balance=before, after_balance=after, events=events)
        assert result.match is True, f"asset {asset} unexpectedly mismatched: {result.explanation}"
        assert all(e.base_asset == asset for e in result.contributing_events), f"asset {asset} was contaminated by another asset's event"
        assert len(result.contributing_events) == 1


# Item 6: multiple rebalances in one session, none contaminating another
# asset's ledger. RVN sell -> finances ZIL; LUNC sell -> finances SAND;
# ZIL sell -> finances MANTRA. ZIL deliberately appears as BOTH an
# arbitrage target (financed by RVN) and a rebalance source (financing
# MANTRA) -- a stronger stress test than one-event-per-asset.


def test_multiple_rebalances_no_cross_contamination():
    events = (
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2107.9, side="SELL"),
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "ZIL", 2626.2711),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "LUNC", -500000.0, side="SELL"),
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "SAND", 300.0),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "ZIL", -1000.0, side="SELL"),
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "MANTRA", 50.0),
    )

    rvn = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=14266.8908, after_balance=14266.8908 - 2107.9, events=events)
    assert rvn.match is True and rvn.expected_delta == pytest.approx(-2107.9)

    zil = reconcile_asset_balance(exchange="binance", asset="ZIL", before_balance=8828.3628, after_balance=8828.3628 + 2626.2711 - 1000.0, events=events)
    assert zil.match is True and zil.expected_delta == pytest.approx(2626.2711 - 1000.0)
    assert len(zil.contributing_events) == 2  # both ZIL events, and only the ZIL events

    lunc = reconcile_asset_balance(exchange="binance", asset="LUNC", before_balance=183339.477, after_balance=183339.477 - 500000.0, events=events)
    assert lunc.match is True and lunc.expected_delta == pytest.approx(-500000.0)

    sand = reconcile_asset_balance(exchange="binance", asset="SAND", before_balance=4.763, after_balance=304.763, events=events)
    assert sand.match is True and sand.expected_delta == pytest.approx(300.0)

    mantra = reconcile_asset_balance(exchange="binance", asset="MANTRA", before_balance=16.636, after_balance=66.636, events=events)
    assert mantra.match is True and mantra.expected_delta == pytest.approx(50.0)

    for result in (rvn, zil, lunc, sand, mantra):
        assert all(e.base_asset == result.asset for e in result.contributing_events)
