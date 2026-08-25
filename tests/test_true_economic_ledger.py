from pathlib import Path

import pytest

from app.execution.true_economic_ledger import (
    apply_buy,
    apply_sell,
    empty_pool,
    get_pool,
    load_state,
    put_pool,
    save_state,
    seed_pool,
    total_unrealized_pnl,
)


def test_empty_pool_has_no_avg_cost():
    assert empty_pool("binance", "RVN").avg_cost_per_unit is None


def test_apply_buy_increases_qty_and_cost_without_mutating_input():
    pool = empty_pool("binance", "RVN")
    new_pool = apply_buy(pool, qty=1000.0, price=0.0033, fee_amount=0.0, fee_asset="USDT")
    assert pool.qty == 0.0  # input untouched
    assert new_pool.qty == 1000.0
    assert new_pool.cost_usd == pytest.approx(3.3)
    assert new_pool.avg_cost_per_unit == pytest.approx(0.0033)


def test_buy_fee_in_base_asset_reduces_net_qty_not_cost():
    """In-kind fee (paid in the asset just bought, e.g. RVN) never touched
    USD at all -- it must reduce net quantity credited to the pool, and
    must NOT also increase cost_usd (that would double-count exactly the
    way live_arbitrage_executor's own FIX 1 comment describes)."""
    pool = empty_pool("binance", "RVN")
    new_pool = apply_buy(pool, qty=1000.0, price=0.0033, fee_amount=1.0, fee_asset="RVN")
    assert new_pool.qty == pytest.approx(999.0)
    assert new_pool.cost_usd == pytest.approx(3.3)  # unchanged -- fee never converted to USD


def test_buy_fee_in_quote_asset_increases_cost_not_qty():
    pool = empty_pool("binance", "RVN")
    new_pool = apply_buy(pool, qty=1000.0, price=0.0033, fee_amount=0.01, fee_asset="USDT")
    assert new_pool.qty == pytest.approx(1000.0)
    assert new_pool.cost_usd == pytest.approx(3.31)


def test_sell_realizes_pnl_against_pool_average_cost_not_current_buy_price():
    """The whole point of this module: a sell realizes gain/loss against
    what THIS pool's units actually cost, never against what it costs to
    buy an equivalent amount somewhere else right now."""
    pool = seed_pool({}, "bybit", "RVN", qty=1000.0, price=0.0030)
    p = get_pool(pool, "bybit", "RVN")
    result = apply_sell(p, qty=400.0, price=0.0035, fee_amount=0.0, fee_asset="USDT")
    assert result is not None
    assert result.cost_basis_of_units_sold_usd == pytest.approx(400.0 * 0.0030)
    assert result.net_proceeds_usd == pytest.approx(400.0 * 0.0035)
    assert result.realized_pnl_usd == pytest.approx(400.0 * (0.0035 - 0.0030))


def test_sell_fee_in_quote_asset_reduces_proceeds():
    pool = seed_pool({}, "bybit", "RVN", qty=1000.0, price=0.0030)
    p = get_pool(pool, "bybit", "RVN")
    result = apply_sell(p, qty=400.0, price=0.0035, fee_amount=0.05, fee_asset="USDT")
    assert result.net_proceeds_usd == pytest.approx(400.0 * 0.0035 - 0.05)
    assert result.realized_pnl_usd == pytest.approx(400.0 * (0.0035 - 0.0030) - 0.05)


def test_sell_fee_in_base_asset_increases_net_qty_removed_from_pool():
    pool = seed_pool({}, "bybit", "RVN", qty=1000.0, price=0.0030)
    p = get_pool(pool, "bybit", "RVN")
    result = apply_sell(p, qty=400.0, price=0.0035, fee_amount=2.0, fee_asset="RVN")
    assert result.net_qty_sold == pytest.approx(402.0)
    assert result.pool.qty == pytest.approx(1000.0 - 402.0)
    # cost basis removed is for the net 402 units, not just the 400 traded
    assert result.cost_basis_of_units_sold_usd == pytest.approx(402.0 * 0.0030)


def test_partial_depletion_leaves_correct_residual_pool():
    pool = seed_pool({}, "binance", "ZIL", qty=1000.0, price=0.00280)
    p = get_pool(pool, "binance", "ZIL")
    result = apply_sell(p, qty=300.0, price=0.00290, fee_amount=0.0, fee_asset="USDT")
    residual = result.pool
    assert residual.qty == pytest.approx(700.0)
    # average cost per unit is unchanged by a sell -- only realized units leave the pool
    assert residual.avg_cost_per_unit == pytest.approx(0.00280)
    assert residual.cost_usd == pytest.approx(700.0 * 0.00280)


def test_multiple_buys_then_partial_sell_uses_correct_weighted_average():
    pool = empty_pool("binance", "LUNC")
    pool = apply_buy(pool, qty=1000.0, price=0.0000500, fee_amount=0.0, fee_asset="USDT")
    pool = apply_buy(pool, qty=2000.0, price=0.0000560, fee_amount=0.0, fee_asset="USDT")
    # weighted avg = (1000*0.00005 + 2000*0.000056) / 3000 = (0.05 + 0.112) / 3000
    expected_avg = (1000 * 0.0000500 + 2000 * 0.0000560) / 3000.0
    assert pool.avg_cost_per_unit == pytest.approx(expected_avg)

    result = apply_sell(pool, qty=1200.0, price=0.0000600, fee_amount=0.0, fee_asset="USDT")
    assert result.cost_basis_of_units_sold_usd == pytest.approx(1200.0 * expected_avg)
    assert result.realized_pnl_usd == pytest.approx(1200.0 * (0.0000600 - expected_avg))
    assert result.pool.qty == pytest.approx(1800.0)
    # average cost of the REMAINING units is unchanged -- a sell never
    # alters the average cost of what's left, only the realized event
    assert result.pool.avg_cost_per_unit == pytest.approx(expected_avg)


def test_selling_more_than_the_pool_holds_returns_none_never_a_fabricated_cost_basis():
    pool = seed_pool({}, "bybit", "LUNC", qty=1311.165, price=0.00005505)
    p = get_pool(pool, "bybit", "LUNC")
    # a real V4 event: attempting to sell 134828 LUNC against a pool that
    # (at session start, before any buys) only ever held 1311.165
    result = apply_sell(p, qty=134828.0, price=0.00005512, fee_amount=0.00743171936, fee_asset="USDT")
    assert result is None


def test_selling_from_an_empty_pool_returns_none():
    pool = empty_pool("binance", "SAND")
    assert apply_sell(pool, qty=1.0, price=0.04, fee_amount=0.0, fee_asset="USDT") is None


def test_rvn_first_v4_cycle_historical_regression():
    """Exact real fill data, first arbitrage cycle of the whole V4
    session (2026-08-25T08:51:59.544Z, attempt_id
    60e19275481245bfbcebbfce). The OLD formula (sell_proceeds - buy_cost,
    same cycle, cross-exchange) reported +0.209185 -- reproduced here for
    contrast, not as this module's output. Bybit's RVN pool at that exact
    instant was the untouched session-starting position: qty=2200.8922 at
    a real historical cost basis of 0.003416/unit (session-start kline)."""
    state = seed_pool({}, "bybit", "RVN", qty=2200.8922, price=0.003416)
    pool = get_pool(state, "bybit", "RVN")
    result = apply_sell(pool, qty=2130.9, price=0.003415, fee_amount=0.0072770235, fee_asset="USDT")
    assert result is not None
    assert result.realized_pnl_usd == pytest.approx(-0.009407923500000415, abs=1e-9)

    # contrast: the OLD V4 formula on the exact same two real fills
    buy_qty, buy_price, buy_fee_usdt = 2133.1, 0.00331, 0.0  # fee was in RVN, never added under the old rule either
    sell_qty, sell_price, sell_fee_usdt = 2130.9, 0.003415, 0.0072770235
    old_buy_cost = buy_qty * buy_price + buy_fee_usdt
    old_sell_proceeds = sell_qty * sell_price - sell_fee_usdt
    old_formula_pnl = old_sell_proceeds - old_buy_cost
    assert old_formula_pnl == pytest.approx(0.209185, abs=1e-6)


def test_persistence_round_trip_survives_a_restart(tmp_path):
    """Item 8, user directive: restart/persistence of cost basis. A
    process crash/restart must never lose or reset the true cost basis --
    the whole point of persisting it durably, matching
    app.operations.order_intent_log's own load_state/save_state pattern."""
    path: Path = tmp_path / "true_economic_ledger.json"
    state = seed_pool({}, "bybit", "RVN", qty=2200.8922, price=0.003416)
    state = put_pool(state, apply_buy(get_pool(state, "binance", "ZIL"), qty=500.0, price=0.0028, fee_amount=1.0, fee_asset="ZIL"))
    save_state(state, path)

    reloaded = load_state(path)
    assert get_pool(reloaded, "bybit", "RVN") == get_pool(state, "bybit", "RVN")
    assert get_pool(reloaded, "binance", "ZIL") == get_pool(state, "binance", "ZIL")

    # and a sell against the reloaded state produces the identical result
    # as against the pre-restart state -- the cost basis truly survived
    p_before = get_pool(state, "bybit", "RVN")
    p_after = get_pool(reloaded, "bybit", "RVN")
    r_before = apply_sell(p_before, qty=100.0, price=0.0034, fee_amount=0.0, fee_asset="USDT")
    r_after = apply_sell(p_after, qty=100.0, price=0.0034, fee_amount=0.0, fee_asset="USDT")
    assert r_before.realized_pnl_usd == pytest.approx(r_after.realized_pnl_usd)


def test_load_state_missing_file_returns_empty_state(tmp_path):
    assert load_state(tmp_path / "does_not_exist.json") == {}


def test_total_unrealized_pnl_sums_qty_times_price_minus_cost():
    state = seed_pool({}, "binance", "RVN", qty=1000.0, price=0.0030)
    state = seed_pool(state, "bybit", "ZIL", qty=500.0, price=0.0028)
    total = total_unrealized_pnl(state, {("binance", "RVN"): 0.0032, ("bybit", "ZIL"): 0.0028})
    expected = (1000.0 * 0.0032 - 1000.0 * 0.0030) + (500.0 * 0.0028 - 500.0 * 0.0028)
    assert total == pytest.approx(expected)


def test_total_unrealized_pnl_skips_pools_with_no_known_price():
    state = seed_pool({}, "binance", "RVN", qty=1000.0, price=0.0030)
    state = seed_pool(state, "bybit", "ZIL", qty=500.0, price=0.0028)
    total = total_unrealized_pnl(state, {("binance", "RVN"): 0.0032})  # ZIL price unknown, must be skipped not zeroed
    assert total == pytest.approx(1000.0 * 0.0032 - 1000.0 * 0.0030)


def test_total_unrealized_pnl_empty_state_is_zero():
    assert total_unrealized_pnl({}, {("binance", "RVN"): 0.0032}) == 0.0
