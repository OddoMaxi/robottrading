import pytest

from app.execution.v4_session_replay import (
    ArbitrageCycleEvent,
    InventoryBuyEvent,
    RebalanceSellEvent,
    build_events_from_fills,
    replay_v4_decisions_through_v5_gate,
    replay_wealth_bridge,
)


_next_order_id = iter(range(1, 100_000))


def _fill(**kw):
    base = {"exchange": "binance", "symbol": "RVNUSDT", "base_asset": "RVN", "side": "BUY", "qty": 0.0,
            "quote_qty": 0.0, "price": 0.0, "commission": 0.0, "commission_asset": "USDT", "ts_ms": 0,
            "client_order_id": "", "order_id": str(next(_next_order_id)), "purpose": "ARBITRAGE_BUY"}
    base.update(kw)
    return base


def test_build_events_pairs_buy_and_sell_by_attempt_id():
    fills = [
        _fill(exchange="binance", side="BUY", qty=1000.0, quote_qty=3.3, price=0.0033, commission=1.0,
              commission_asset="RVN", ts_ms=100, client_order_id="buy-abc", purpose="ARBITRAGE_BUY"),
        _fill(exchange="bybit", side="SELL", qty=998.0, quote_qty=3.36, price=0.00337, commission=0.00336,
              commission_asset="USDT", ts_ms=101, client_order_id="sell-abc", purpose="ARBITRAGE_SELL"),
    ]
    events = build_events_from_fills(fills)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, ArbitrageCycleEvent)
    assert e.attempt_id == "abc"
    assert e.buy_exchange == "binance" and e.sell_exchange == "bybit"
    assert e.ts_ms == 100  # earliest of the two


def test_build_events_aggregates_multi_fill_orders_to_notional_weighted_average():
    fills = [
        _fill(side="BUY", qty=1000.0, quote_qty=3.3, price=0.0033, commission=0.0, ts_ms=10, client_order_id="buy-x", purpose="ARBITRAGE_BUY"),
        _fill(side="BUY", qty=500.0, quote_qty=1.7, price=0.0034, commission=0.0, ts_ms=10, client_order_id="buy-x", purpose="ARBITRAGE_BUY"),
        _fill(exchange="bybit", side="SELL", qty=1490.0, quote_qty=5.06, price=0.0034, commission=0.0, ts_ms=11, client_order_id="sell-x", purpose="ARBITRAGE_SELL"),
    ]
    events = build_events_from_fills(fills)
    e = events[0]
    assert e.buy_qty == pytest.approx(1500.0)
    assert e.buy_price == pytest.approx((3.3 + 1.7) / 1500.0)


def test_build_events_aggregates_multi_fill_rebalance_order_to_one_event():
    """A single real rebalance order can partial-fill more than once
    (especially on Bybit) -- these must collapse into ONE event so
    REBALANCES SEEN matches V4's own per-order counters, not an inflated
    per-fill count."""
    fills = [
        _fill(side="SELL", purpose="REBALANCE_SELL", qty=30.0, quote_qty=30.0 * 0.0033, price=0.0033, commission=0.0, ts_ms=6, order_id="777"),
        _fill(side="SELL", purpose="REBALANCE_SELL", qty=20.0, quote_qty=20.0 * 0.0034, price=0.0034, commission=0.0, ts_ms=7, order_id="777"),
    ]
    events = build_events_from_fills(fills)
    assert len(events) == 1
    e = events[0]
    assert isinstance(e, RebalanceSellEvent)
    assert e.qty == pytest.approx(50.0)
    assert e.ts_ms == 6


def test_build_events_keeps_inventory_and_rebalance_as_standalone_events():
    fills = [
        _fill(side="BUY", purpose="INVENTORY_BUY", qty=100.0, price=0.003, commission=0.1, commission_asset="RVN", ts_ms=5),
        _fill(side="SELL", purpose="REBALANCE_SELL", qty=50.0, price=0.0033, commission=0.01, commission_asset="USDT", ts_ms=6),
    ]
    events = build_events_from_fills(fills)
    assert len(events) == 2
    assert isinstance(events[0], InventoryBuyEvent)
    assert isinstance(events[1], RebalanceSellEvent)


def test_build_events_ignores_unpaired_buy_or_sell_orphans():
    """An unmatched buy-{id} with no corresponding sell-{id} (e.g. a
    neutralization-only leg) must not silently become a phantom cycle."""
    fills = [_fill(side="BUY", ts_ms=1, client_order_id="buy-orphan", purpose="ARBITRAGE_BUY")]
    assert build_events_from_fills(fills) == []


def test_wealth_bridge_reproduces_a_simple_hand_checkable_scenario():
    fills = [
        _fill(exchange="bybit", side="SELL", qty=400.0, quote_qty=400.0 * 0.0035, price=0.0035, commission=0.0, ts_ms=1),
    ]
    starting = {("bybit", "RVN"): (1000.0, 0.0030)}
    current_prices = {("bybit", "RVN"): 0.0032}
    result = replay_wealth_bridge(fills, starting, current_prices)
    assert result.total_realized_pnl_usd == pytest.approx(400.0 * (0.0035 - 0.0030))
    # remaining 600 units, cost basis 0.0030, marked at 0.0032
    assert result.total_unrealized_pnl_usd == pytest.approx(600.0 * (0.0032 - 0.0030))
    assert result.total_wealth_change_usd == pytest.approx(result.total_realized_pnl_usd + result.total_unrealized_pnl_usd)


def test_wealth_bridge_untouched_asset_still_contributes_pure_price_effect():
    """MANTRA this V4 session: zero fills, but its value still moved with
    the market -- the bridge must reflect that even with no trade data
    for it."""
    starting = {("binance", "MANTRA"): (16.636, 0.00424)}
    current_prices = {("binance", "MANTRA"): 0.00422}
    result = replay_wealth_bridge([], starting, current_prices)
    assert result.total_realized_pnl_usd == pytest.approx(0.0)
    assert result.total_unrealized_pnl_usd == pytest.approx(16.636 * (0.00422 - 0.00424))


def test_v5_gate_rejects_a_cycle_whose_sell_side_cost_basis_exceeds_proceeds():
    events = [
        ArbitrageCycleEvent(
            attempt_id="a1", ts_ms=1, symbol="RVNUSDT", base_asset="RVN", buy_exchange="binance", sell_exchange="bybit",
            buy_qty=1000.0, buy_price=0.0033, buy_fee_amount=0.0, buy_fee_asset="USDT",
            sell_qty=1000.0, sell_price=0.0034, sell_fee_amount=0.0, sell_fee_asset="USDT",
        ),
    ]
    starting_pools = {("bybit", "RVN"): (5000.0, 0.0040), ("binance", "RVN"): (0.0, 0.0)}  # bybit pool already cost 0.0040/unit -- above the 0.0034 sell price
    starting_usdt = {"binance": 100.0, "bybit": 100.0}
    report = replay_v4_decisions_through_v5_gate(events, starting_pools, starting_usdt)
    assert report.accepted_count == 0
    assert report.rejected_count == 1
    assert report.decisions[0].true_wealth_delta_usd < 0


def test_v5_gate_accepts_a_genuinely_profitable_cycle_and_updates_simulated_usdt():
    events = [
        ArbitrageCycleEvent(
            attempt_id="a1", ts_ms=1, symbol="RVNUSDT", base_asset="RVN", buy_exchange="binance", sell_exchange="bybit",
            buy_qty=1000.0, buy_price=0.0030, buy_fee_amount=0.0, buy_fee_asset="USDT",
            sell_qty=1000.0, sell_price=0.0034, sell_fee_amount=0.0, sell_fee_asset="USDT",
        ),
    ]
    starting_pools = {("bybit", "RVN"): (5000.0, 0.0028), ("binance", "RVN"): (0.0, 0.0)}
    starting_usdt = {"binance": 100.0, "bybit": 100.0}
    report = replay_v4_decisions_through_v5_gate(events, starting_pools, starting_usdt)
    assert report.accepted_count == 1
    assert report.accepted_true_pnl_usd > 0


def test_v5_gate_rebalance_avoided_when_simulated_balance_is_healthy():
    events = [RebalanceSellEvent(ts_ms=1, exchange="binance", asset="ZIL", qty=100.0, price=0.0028, fee_amount=0.0, fee_asset="USDT")]
    starting_pools = {("binance", "ZIL"): (1000.0, 0.0028)}
    starting_usdt = {"binance": 100.0, "bybit": 100.0}  # comfortably above the 25.0 default floor
    report = replay_v4_decisions_through_v5_gate(events, starting_pools, starting_usdt)
    assert report.rebalances_seen == 1
    assert report.rebalances_avoided == 1


def test_v5_gate_rebalance_still_applied_when_simulated_balance_is_below_floor():
    events = [RebalanceSellEvent(ts_ms=1, exchange="binance", asset="ZIL", qty=100.0, price=0.0028, fee_amount=0.0, fee_asset="USDT")]
    starting_pools = {("binance", "ZIL"): (1000.0, 0.0028)}
    starting_usdt = {"binance": 5.0, "bybit": 100.0}  # below the 25.0 default floor
    report = replay_v4_decisions_through_v5_gate(events, starting_pools, starting_usdt)
    assert report.rebalances_avoided == 0


def test_v5_gate_inventory_action_avoided_when_pool_already_ample():
    events = [InventoryBuyEvent(ts_ms=1, exchange="bybit", asset="RVN", qty=100.0, price=0.0033, fee_amount=0.0, fee_asset="USDT")]
    starting_pools = {("bybit", "RVN"): (5000.0, 0.0030)}  # already far more than the 100.0 this buy would add
    starting_usdt = {"binance": 100.0, "bybit": 100.0}
    report = replay_v4_decisions_through_v5_gate(events, starting_pools, starting_usdt)
    assert report.inventory_actions_seen == 1
    assert report.inventory_actions_avoided == 1


def test_v5_gate_inventory_action_still_applied_when_pool_is_thin():
    events = [InventoryBuyEvent(ts_ms=1, exchange="bybit", asset="RVN", qty=100.0, price=0.0033, fee_amount=0.0, fee_asset="USDT")]
    starting_pools = {("bybit", "RVN"): (10.0, 0.0030)}
    starting_usdt = {"binance": 100.0, "bybit": 100.0}
    report = replay_v4_decisions_through_v5_gate(events, starting_pools, starting_usdt)
    assert report.inventory_actions_avoided == 0
