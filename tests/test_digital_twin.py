from app.execution.digital_twin import (
    attempt_arbitrage_on_twin, attempt_inventory_constitution_on_twin, bootstrap_mode_a, bootstrap_mode_b,
    compute_simulated_liquidation_net_worth, maybe_rebalance_on_twin,
)
from app.execution.dual_leg_quote import LegSnapshot


def _leg(exchange, side, bid, ask, depth_qty=10_000.0, min_notional=5.0, min_qty=1.0):
    price = ask if side == "buy" else bid
    return LegSnapshot(
        exchange=exchange, side=side, best_bid=bid, best_ask=ask,
        depth_levels=[(price, depth_qty)], min_qty=min_qty, step_size=0.1, tick_size=0.0001,
        min_notional=min_notional, tradable=True, maker_fee_rate=0.001, taker_fee_rate=0.001,
        fee_source="real_account_fee", fetch_started_at=0.0, fetch_completed_at=0.0,
    )


def _profitable_legs():
    return _leg("binance", "buy", bid=0.999, ask=1.00), _leg("bybit", "sell", bid=1.05, ask=1.051)


def test_bootstrap_mode_a_seeds_usdt_and_inventory_from_real_state():
    state = bootstrap_mode_a(
        real_usdt_by_exchange={"binance": 100.0, "bybit": 50.0, "okx": 20.0},
        real_balances_by_exchange={"binance": {"XYZ": 10.0}, "bybit": {}, "okx": {}},
        real_prices_by_exchange={"binance": {"XYZ": 2.0}},
    )
    assert state.usdt == {"binance": 100.0, "bybit": 50.0, "okx": 20.0}
    from app.execution.true_economic_ledger import get_pool
    pool = get_pool(state.ledger, "binance", "XYZ")
    assert pool.qty == 10.0
    assert pool.cost_usd == 20.0


def test_bootstrap_mode_b_never_seeds_inventory():
    state = bootstrap_mode_b(1000.0, {"binance": 0.4, "bybit": 0.4, "okx": 0.2})
    assert state.ledger == {}
    assert abs(state.usdt["binance"] - 400.0) < 1e-9
    assert abs(state.usdt["bybit"] - 400.0) < 1e-9
    assert abs(state.usdt["okx"] - 200.0) < 1e-9
    assert abs(sum(state.usdt.values()) - 1000.0) < 1e-6


def test_simulated_liquidation_net_worth_sums_usdt_and_inventory():
    state = bootstrap_mode_a(
        real_usdt_by_exchange={"binance": 100.0, "bybit": 0.0},
        real_balances_by_exchange={"binance": {}, "bybit": {"XYZ": 10.0}},
        real_prices_by_exchange={"bybit": {"XYZ": 2.0}},
    )
    nw = compute_simulated_liquidation_net_worth(state, {"binance": {}, "bybit": {"XYZ": 2.5}})
    assert abs(nw - (100.0 + 10.0 * 2.5)) < 1e-9


def test_arbitrage_blocked_by_inventory_missing_when_sell_pool_empty():
    state = bootstrap_mode_b(1000.0, {"binance": 0.5, "bybit": 0.5})
    buy_leg, sell_leg = _profitable_legs()
    result = attempt_arbitrage_on_twin(
        state, buy_exchange="binance", sell_exchange="bybit", base_asset="XYZ",
        buy_leg=buy_leg, sell_leg=sell_leg, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    assert result.accepted is False
    assert result.blocker == "INVENTORY_MISSING"
    assert result.new_state is state  # unchanged


def test_arbitrage_accepted_mutates_state_correctly():
    state = bootstrap_mode_a(
        real_usdt_by_exchange={"binance": 1000.0, "bybit": 1000.0},
        real_balances_by_exchange={"binance": {}, "bybit": {"XYZ": 1000.0}},
        real_prices_by_exchange={"bybit": {"XYZ": 0.5}},
    )
    buy_leg, sell_leg = _profitable_legs()
    result = attempt_arbitrage_on_twin(
        state, buy_exchange="binance", sell_exchange="bybit", base_asset="XYZ",
        buy_leg=buy_leg, sell_leg=sell_leg, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    assert result.accepted is True
    assert result.te_quote.expected_true_wealth_delta_usd > 0
    # binance (buy exchange) spent USDT, bybit (sell exchange) received proceeds
    assert result.new_state.usdt["binance"] < state.usdt["binance"]
    assert result.new_state.usdt["bybit"] > state.usdt["bybit"]
    # original state is untouched (pure/immutable)
    assert state.usdt["binance"] == 1000.0


def test_arbitrage_blocked_by_reserve_floor():
    state = bootstrap_mode_a(
        real_usdt_by_exchange={"binance": 15.0, "bybit": 1000.0},  # just above the $10 trade but the floor is 20
        real_balances_by_exchange={"binance": {}, "bybit": {"XYZ": 1000.0}},
        real_prices_by_exchange={"bybit": {"XYZ": 0.5}},
    )
    buy_leg, sell_leg = _profitable_legs()
    result = attempt_arbitrage_on_twin(
        state, buy_exchange="binance", sell_exchange="bybit", base_asset="XYZ",
        buy_leg=buy_leg, sell_leg=sell_leg, reserve_floor_usd=20.0, notional_usd=10.0,
    )
    assert result.accepted is False
    assert "RESERVE_FLOOR" in result.blocker
    assert result.new_state is state


def test_inventory_constitution_accepted_creates_real_pool():
    state = bootstrap_mode_b(1000.0, {"binance": 1.0})
    result = attempt_inventory_constitution_on_twin(
        state, exchange="binance", asset="XYZ", qty=10.0, ask_price=1.00, mark_price=1.05, fee_amount=0.01,
    )
    assert result.accepted is True
    from app.execution.true_economic_ledger import get_pool
    pool = get_pool(result.new_state.ledger, "binance", "XYZ")
    assert pool.qty == 10.0
    assert result.new_state.usdt["binance"] < 1000.0


def test_inventory_constitution_rejected_when_insufficient_simulated_usdt():
    state = bootstrap_mode_b(5.0, {"binance": 1.0})
    result = attempt_inventory_constitution_on_twin(
        state, exchange="binance", asset="XYZ", qty=1000.0, ask_price=1.00, mark_price=1.05, fee_amount=1.0,
    )
    assert result.accepted is False
    assert result.new_state is state


def test_rebalance_noop_when_already_above_floor():
    state = bootstrap_mode_a(
        real_usdt_by_exchange={"binance": 100.0}, real_balances_by_exchange={"binance": {"XYZ": 10.0}},
        real_prices_by_exchange={"binance": {"XYZ": 1.0}},
    )
    result = maybe_rebalance_on_twin(state, exchange="binance", reserve_floor_usd=20.0, asset="XYZ", sell_price=1.0, fee_rate=0.001, upcoming_trade_notional_usd=10.0)
    assert result.performed is False
    assert result.new_state is state


def test_rebalance_sells_just_enough_to_restore_floor():
    state = bootstrap_mode_a(
        real_usdt_by_exchange={"binance": 15.0}, real_balances_by_exchange={"binance": {"XYZ": 100.0}},
        real_prices_by_exchange={"binance": {"XYZ": 1.0}},
    )
    result = maybe_rebalance_on_twin(state, exchange="binance", reserve_floor_usd=25.0, asset="XYZ", sell_price=1.0, fee_rate=0.0, upcoming_trade_notional_usd=0.0)
    assert result.performed is True
    assert result.new_state.usdt["binance"] >= 25.0 - 1e-6
    from app.execution.true_economic_ledger import get_pool
    pool = get_pool(result.new_state.ledger, "binance", "XYZ")
    assert pool.qty < 100.0  # sold some, not all
