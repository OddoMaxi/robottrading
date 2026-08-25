import uuid

from app.execution.dual_leg_quote import LegSnapshot
from app.execution.true_economic_ledger import CostBasisPool, empty_pool, seed_pool, get_pool
from app.reporting.simulation_vs_real_comparator import (
    classify_difference, compute_real_side, compute_sim_side, recompute_sim_true_economic,
)

QUOTE = "USDT"


def _leg(exchange, side, bid, ask, depth_price, depth_qty=10_000.0, min_notional=5.0, min_qty=1.0):
    price = ask if side == "buy" else bid
    return LegSnapshot(
        exchange=exchange, side=side, best_bid=bid, best_ask=ask,
        depth_levels=[(price, depth_qty), (price * (1.001 if side == "buy" else 0.999), depth_qty)],
        min_qty=min_qty, step_size=0.1, tick_size=0.0001, min_notional=min_notional, tradable=True,
        maker_fee_rate=0.001, taker_fee_rate=0.001, fee_source="real_account_fee",
        fetch_started_at=0.0, fetch_completed_at=0.0,
    )


def _profitable_legs():
    # buy at 1.00 on A, sell at 1.05 on B -- a real, crossable 5% spread
    buy_leg = _leg("binance", "buy", bid=0.999, ask=1.00, depth_price=1.00)
    sell_leg = _leg("bybit", "sell", bid=1.05, ask=1.051, depth_price=1.05)
    return buy_leg, sell_leg


def _unprofitable_legs():
    buy_leg = _leg("binance", "buy", bid=1.049, ask=1.05, depth_price=1.05)
    sell_leg = _leg("bybit", "sell", bid=1.00, ask=1.001, depth_price=1.00)
    return buy_leg, sell_leg


def test_sim_side_detects_and_prices_a_real_crossable_spread():
    buy_leg, sell_leg = _profitable_legs()
    result = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    assert result.detected is True
    assert result.would_trade is True
    assert result.net_pnl_usd is not None and result.net_pnl_usd > 0
    assert result.notional_usd is not None and result.notional_usd > 0
    assert result.inventory_required_qty is None  # SIM never models inventory -- always disclosed as None


def test_sim_side_not_detected_when_no_crossable_spread():
    buy_leg, sell_leg = _unprofitable_legs()
    result = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    assert result.detected is False
    assert result.would_trade is False
    assert result.rejection_reason is not None


def test_real_side_blocked_by_unknown_cost_basis_when_sell_exchange_has_zero_inventory():
    buy_leg, sell_leg = _profitable_legs()
    sell_pool = empty_pool("bybit", "XYZ")
    buy_pool = empty_pool("binance", "XYZ")
    result = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=sell_pool, buy_pool=buy_pool,
        real_buy_balance_usd=1000.0, real_sell_inventory_qty=0.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    assert result.would_trade is False
    assert result.sell_side_cost_basis_usd is None  # unknown -- never fabricated from an empty pool
    assert result.rejection_reason == "NOT_TRUE_ECONOMIC_POSITIVE"  # matches evaluate_executability's own precedence: an unknown cost basis makes would_trade=False upstream


def test_real_side_executable_with_real_inventory_and_capital():
    buy_leg, sell_leg = _profitable_legs()
    sell_pool = seed_pool({}, "bybit", "XYZ", qty=1000.0, price=0.5)
    buy_pool = empty_pool("binance", "XYZ")
    result = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=get_pool(sell_pool, "bybit", "XYZ"), buy_pool=buy_pool,
        real_buy_balance_usd=1000.0, real_sell_inventory_qty=1000.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    assert result.would_trade is True
    assert result.true_economic_pnl_usd is not None and result.true_economic_pnl_usd > 0


def test_real_side_blocked_by_insufficient_capital():
    buy_leg, sell_leg = _profitable_legs()
    sell_pool = get_pool(seed_pool({}, "bybit", "XYZ", qty=1000.0, price=0.5), "bybit", "XYZ")
    buy_pool = empty_pool("binance", "XYZ")
    result = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=sell_pool, buy_pool=buy_pool,
        real_buy_balance_usd=0.50, real_sell_inventory_qty=1000.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    assert result.would_trade is False
    assert result.rejection_reason == "INSUFFICIENT_CAPITAL"


def test_recompute_sim_true_economic_reveals_accounting_bias_with_underwater_inventory():
    buy_leg, sell_leg = _profitable_legs()
    sim = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    assert sim.would_trade is True and sim.net_pnl_usd > 0

    # the sell exchange's REAL inventory was acquired at 2.00 -- far above
    # the 1.05 it's about to be "sold" for in this opportunity -- an
    # underwater position simulation's free-inventory model never sees.
    underwater_sell_pool = get_pool(seed_pool({}, "bybit", "XYZ", qty=100_000.0, price=2.00), "bybit", "XYZ")
    empty_buy_pool = empty_pool("binance", "XYZ")

    recalculated = recompute_sim_true_economic(
        sim=sim, sell_pool=underwater_sell_pool, buy_pool=empty_buy_pool, buy_side_mark_price=buy_leg.best_bid,
    )
    assert recalculated is not None
    assert recalculated < sim.net_pnl_usd  # true economics is strictly worse than the free-inventory sim number
    assert recalculated < 0  # the underwater cost basis flips this specific case fully negative


def test_recompute_sim_true_economic_is_none_when_sim_never_priced_a_trade():
    buy_leg, sell_leg = _unprofitable_legs()
    sim = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    result = recompute_sim_true_economic(
        sim=sim, sell_pool=empty_pool("bybit", "XYZ"), buy_pool=empty_pool("binance", "XYZ"), buy_side_mark_price=buy_leg.best_bid,
    )
    assert result is None


def test_classify_difference_none_when_both_agree():
    buy_leg, sell_leg = _profitable_legs()
    sim = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    sell_pool = get_pool(seed_pool({}, "bybit", "XYZ", qty=1000.0, price=0.5), "bybit", "XYZ")
    real = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=sell_pool, buy_pool=empty_pool("binance", "XYZ"),
        real_buy_balance_usd=1000.0, real_sell_inventory_qty=1000.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    assert classify_difference(sim=sim, real=real, sim_recalculated_true_economic_pnl=1.0, real_sell_inventory_qty=1000.0) is None


def test_classify_difference_flags_missing_inventory_as_primary_cause():
    buy_leg, sell_leg = _profitable_legs()
    sim = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    real = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=empty_pool("bybit", "XYZ"), buy_pool=empty_pool("binance", "XYZ"),
        real_buy_balance_usd=1000.0, real_sell_inventory_qty=0.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    attribution = classify_difference(sim=sim, real=real, sim_recalculated_true_economic_pnl=None, real_sell_inventory_qty=0.0)
    assert attribution is not None
    assert attribution.primary_cause == "SIMULATION_ASSUMED_IMPOSSIBLE_INVENTORY"
    assert "SELL_INVENTORY_MISSING" in attribution.secondary_causes


def test_classify_difference_flags_accounting_bias_when_sim_positive_but_recalc_negative():
    buy_leg, sell_leg = _profitable_legs()
    sim = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    underwater_sell_pool = get_pool(seed_pool({}, "bybit", "XYZ", qty=100_000.0, price=2.00), "bybit", "XYZ")
    real = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=underwater_sell_pool, buy_pool=empty_pool("binance", "XYZ"),
        real_buy_balance_usd=1000.0, real_sell_inventory_qty=1000.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    recalculated = recompute_sim_true_economic(sim=sim, sell_pool=underwater_sell_pool, buy_pool=empty_pool("binance", "XYZ"), buy_side_mark_price=buy_leg.best_bid)
    attribution = classify_difference(sim=sim, real=real, sim_recalculated_true_economic_pnl=recalculated, real_sell_inventory_qty=1000.0)
    assert attribution is not None
    assert "SIMULATION_ACCOUNTING_BIAS" in (attribution.primary_cause, *attribution.secondary_causes)


def test_classify_difference_flags_insufficient_capital():
    buy_leg, sell_leg = _profitable_legs()
    sim = compute_sim_side(buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg)
    sell_pool = get_pool(seed_pool({}, "bybit", "XYZ", qty=1000.0, price=0.5), "bybit", "XYZ")
    real = compute_real_side(
        symbol="XYZ/USDT", buy_exchange="binance", sell_exchange="bybit", buy_leg=buy_leg, sell_leg=sell_leg,
        opportunity_id=uuid.uuid4(), sell_pool=sell_pool, buy_pool=empty_pool("binance", "XYZ"),
        real_buy_balance_usd=0.10, real_sell_inventory_qty=1000.0, reserve_floor_usd=0.0, notional_usd=10.0,
    )
    attribution = classify_difference(sim=sim, real=real, sim_recalculated_true_economic_pnl=1.0, real_sell_inventory_qty=1000.0)
    assert attribution is not None
    assert "INSUFFICIENT_BUY_CAPITAL" in (attribution.primary_cause, *attribution.secondary_causes)
    assert "SIMULATION_ASSUMED_IMPOSSIBLE_CAPITAL" in (attribution.primary_cause, *attribution.secondary_causes)
