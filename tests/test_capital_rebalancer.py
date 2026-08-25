import pytest

from app.execution.capital_rebalancer import (
    InventoryAction,
    ReplayEvent,
    TradeDecision,
    classify_inventory_position,
    compute_capital_imbalance_score,
    compute_rebalance_realized_pnl,
    compute_reserve_floor,
    decide_trade_with_reserve_check,
    evaluate_reserve_impact,
    is_rebalance_needed,
    simulate_event_sequence,
)

# Real observed state (2026-08-25 audit) — used throughout as the
# grounding scenario, not invented numbers.
REAL_BINANCE_USDT = 2.65646726
REAL_BYBIT_USDT = 36.37667066
REAL_MAX_NOTIONAL_PER_LEG = 10.0


# ---- compute_reserve_floor ---------------------------------------------


def test_reserve_floor_at_deployed_default_lands_at_25():
    assert compute_reserve_floor(REAL_MAX_NOTIONAL_PER_LEG) == 25.0


def test_reserve_floor_clamped_to_max_when_cap_is_large():
    assert compute_reserve_floor(100.0) == 25.0


def test_reserve_floor_clamped_to_min_when_cap_is_tiny():
    assert compute_reserve_floor(1.0) == 20.0


def test_reserve_floor_scales_with_multiplier():
    assert compute_reserve_floor(10.0, multiplier=2.0, min_floor=0.0, max_floor=1000.0) == 20.0


# ---- compute_capital_imbalance_score -----------------------------------


def test_imbalance_score_real_state_is_high():
    score = compute_capital_imbalance_score(REAL_BINANCE_USDT, REAL_BYBIT_USDT)
    assert score == pytest.approx(abs(REAL_BINANCE_USDT - REAL_BYBIT_USDT) / (REAL_BINANCE_USDT + REAL_BYBIT_USDT))
    assert score > 0.8  # heavily lopsided toward Bybit


def test_imbalance_score_zero_when_perfectly_balanced():
    assert compute_capital_imbalance_score(50.0, 50.0) == 0.0


def test_imbalance_score_zero_when_no_capital_at_all():
    assert compute_capital_imbalance_score(0.0, 0.0) == 0.0


def test_imbalance_score_bounded_by_one():
    assert compute_capital_imbalance_score(100.0, 0.0) == 1.0


# ---- is_rebalance_needed -------------------------------------------------


def test_rebalance_needed_real_state():
    assert is_rebalance_needed(REAL_BINANCE_USDT, REAL_BYBIT_USDT, 25.0, 25.0) is True  # Binance is far under


def test_rebalance_not_needed_when_both_above_floor():
    assert is_rebalance_needed(30.0, 40.0, 25.0, 25.0) is False


def test_rebalance_needed_when_only_one_side_is_short():
    assert is_rebalance_needed(30.0, 10.0, 25.0, 25.0) is True


# ---- evaluate_reserve_impact ---------------------------------------------


def test_reserve_impact_no_breach():
    impact = evaluate_reserve_impact(current_usdt=50.0, reserve_floor=25.0, trade_notional_usdt=10.0)
    assert impact.would_breach is False
    assert impact.post_trade_usdt == 40.0
    assert impact.shortfall_usdt == 0.0


def test_reserve_impact_breach():
    impact = evaluate_reserve_impact(current_usdt=30.0, reserve_floor=25.0, trade_notional_usdt=10.0)
    assert impact.would_breach is True
    assert impact.post_trade_usdt == 20.0
    assert impact.shortfall_usdt == pytest.approx(5.0)


def test_reserve_impact_real_binance_state_any_trade_breaches():
    """The exact real incident: Binance had 2.66 USDT, floor is 25 —
    even a tiny trade leaves it far under."""
    impact = evaluate_reserve_impact(current_usdt=REAL_BINANCE_USDT, reserve_floor=25.0, trade_notional_usdt=7.0)
    assert impact.would_breach is True
    assert impact.shortfall_usdt > 20.0


# ---- decide_trade_with_reserve_check -------------------------------------


def test_decision_proceeds_when_no_breach():
    result = decide_trade_with_reserve_check(buy_exchange_usdt=50.0, buy_exchange_floor=25.0, trade_notional_usdt=10.0)
    assert result.decision == TradeDecision.ALLOW


def test_decision_prefers_opposite_direction_when_available():
    result = decide_trade_with_reserve_check(
        buy_exchange_usdt=REAL_BINANCE_USDT, buy_exchange_floor=25.0, trade_notional_usdt=7.0,
        opposite_direction_available_and_profitable=True,
    )
    assert result.decision == TradeDecision.PREFER_OPPOSITE_DIRECTION


def test_decision_rebalances_first_when_enough_inventory_reconvertible():
    result = decide_trade_with_reserve_check(
        buy_exchange_usdt=REAL_BINANCE_USDT, buy_exchange_floor=25.0, trade_notional_usdt=7.0,
        opposite_direction_available_and_profitable=False,
        reconvertible_inventory_value_usdt_on_buy_exchange=50.0,  # comfortably more than the ~29 USDT shortfall
    )
    assert result.decision == TradeDecision.REBALANCE_FIRST


def test_decision_do_not_trade_when_no_alternative():
    result = decide_trade_with_reserve_check(
        buy_exchange_usdt=REAL_BINANCE_USDT, buy_exchange_floor=25.0, trade_notional_usdt=7.0,
        opposite_direction_available_and_profitable=False,
        reconvertible_inventory_value_usdt_on_buy_exchange=0.0,
    )
    assert result.decision == TradeDecision.DO_NOT_TRADE


def test_decision_do_not_trade_when_reconvertible_inventory_insufficient():
    result = decide_trade_with_reserve_check(
        buy_exchange_usdt=REAL_BINANCE_USDT, buy_exchange_floor=25.0, trade_notional_usdt=7.0,
        reconvertible_inventory_value_usdt_on_buy_exchange=1.0,  # far short of the ~29 USDT shortfall
    )
    assert result.decision == TradeDecision.DO_NOT_TRADE


def test_decision_opposite_direction_takes_priority_over_rebalance():
    result = decide_trade_with_reserve_check(
        buy_exchange_usdt=REAL_BINANCE_USDT, buy_exchange_floor=25.0, trade_notional_usdt=7.0,
        opposite_direction_available_and_profitable=True,
        reconvertible_inventory_value_usdt_on_buy_exchange=100.0,
    )
    assert result.decision == TradeDecision.PREFER_OPPOSITE_DIRECTION


# ---- classify_inventory_position ------------------------------------------


def test_inventory_dust_below_min_notional():
    decision = classify_inventory_position(value_usdt=0.20, min_notional=5.0, currently_qualifying=False, exchange_below_floor=True, is_top_reconversion_candidate_on_this_exchange=False)
    assert decision.action == InventoryAction.DUST


def test_inventory_dust_takes_priority_even_if_it_would_otherwise_be_top_candidate():
    decision = classify_inventory_position(value_usdt=0.20, min_notional=5.0, currently_qualifying=False, exchange_below_floor=True, is_top_reconversion_candidate_on_this_exchange=True)
    assert decision.action == InventoryAction.DUST


def test_inventory_sell_to_usdt_when_top_candidate_on_depleted_exchange():
    """The real Binance RVN position (~79.5 USDT) — the single largest
    reconvertible holding on the exchange that is short of its floor."""
    decision = classify_inventory_position(value_usdt=79.5, min_notional=5.0, currently_qualifying=True, exchange_below_floor=True, is_top_reconversion_candidate_on_this_exchange=True)
    assert decision.action == InventoryAction.SELL_TO_USDT


def test_inventory_reuse_when_qualifying_and_not_needed_for_rebalance():
    decision = classify_inventory_position(value_usdt=9.7, min_notional=5.0, currently_qualifying=True, exchange_below_floor=True, is_top_reconversion_candidate_on_this_exchange=False)
    assert decision.action == InventoryAction.REUSE


def test_inventory_keep_when_no_pressure_and_not_qualifying():
    decision = classify_inventory_position(value_usdt=9.8, min_notional=5.0, currently_qualifying=False, exchange_below_floor=False, is_top_reconversion_candidate_on_this_exchange=False)
    assert decision.action == InventoryAction.KEEP


# ---- compute_rebalance_realized_pnl ---------------------------------------


def test_rebalance_pnl_profit_when_sold_above_cost_basis():
    pnl = compute_rebalance_realized_pnl(qty_sold=100.0, sell_price_usdt=0.01, cost_basis_usdt_per_unit=0.008, fee_usd=0.01)
    assert pnl == pytest.approx((0.01 - 0.008) * 100.0 - 0.01)
    assert pnl > 0


def test_rebalance_pnl_loss_when_sold_below_cost_basis():
    pnl = compute_rebalance_realized_pnl(qty_sold=100.0, sell_price_usdt=0.007, cost_basis_usdt_per_unit=0.008, fee_usd=0.01)
    assert pnl < 0


def test_rebalance_pnl_none_when_cost_basis_unknown():
    """Never fabricate a P&L figure without a real cost basis to compare against."""
    assert compute_rebalance_realized_pnl(qty_sold=100.0, sell_price_usdt=0.01, cost_basis_usdt_per_unit=None, fee_usd=0.01) is None


def test_rebalance_pnl_matches_the_directive_example():
    """"un arbitrage a +0.20 USDT suivi d'un rebalance coutant -0.05 USDT
    doit contribuer seulement +0.15 USDT" -- the rebalance's own P&L must
    be a real, sign-correct number that nets against the arbitrage's."""
    arbitrage_pnl = 0.20
    rebalance_pnl = compute_rebalance_realized_pnl(qty_sold=10.0, sell_price_usdt=0.995, cost_basis_usdt_per_unit=1.0, fee_usd=0.0)
    assert rebalance_pnl == pytest.approx(-0.05)
    assert (arbitrage_pnl + rebalance_pnl) == pytest.approx(0.15)


# ---- simulate_event_sequence (the 31-event historical replay lives in
# tests/test_capital_rebalancer_replay.py; these are smaller, synthetic
# sanity checks of the mechanics themselves) --------------------------------


def _event(at, spend_exchange, spend_notional, price, qty_received=None, receive_exchange=None, receive_notional=0.0, qty_sold=0.0, symbol="RVNUSDT", kind="ARBITRAGE"):
    return ReplayEvent(
        at=at, kind=kind, symbol=symbol, spend_exchange=spend_exchange, receive_exchange=receive_exchange,
        spend_notional_usdt=spend_notional, receive_notional_usdt=receive_notional, price_usdt=price,
        qty_received_net=qty_received if qty_received is not None else spend_notional / price, qty_sold=qty_sold,
    )


def test_simulate_sequence_allows_trades_within_the_floor():
    events = [_event(1, "binance", 5.0, 0.01), _event(2, "binance", 5.0, 0.01)]
    result = simulate_event_sequence(events, starting_binance_usdt=100.0, starting_bybit_usdt=60.0, binance_floor=25.0, bybit_floor=25.0)
    assert result.interventions == 0
    assert result.min_binance_usdt == pytest.approx(90.0)
    assert all(s.decision.decision.value == "ALLOW" for s in result.steps)


def test_simulate_sequence_never_breaches_the_floor_with_repeated_same_direction_spending():
    """The exact shape of the real incident: many consecutive events
    spending USDT on the same exchange. Even starting close to the
    floor, the simulated balance must never drop meaningfully below it
    once enough same-exchange inventory has accumulated to rebalance."""
    events = [_event(i, "binance", 7.0, 0.0033, qty_received=7.0 / 0.0033) for i in range(20)]
    result = simulate_event_sequence(events, starting_binance_usdt=30.0, starting_bybit_usdt=60.0, binance_floor=25.0, bybit_floor=25.0)
    assert result.min_binance_usdt >= 25.0 - 0.5  # small, disclosed taker-fee slack only
    assert result.interventions > 0  # the floor genuinely had to intervene at least once


def test_simulate_sequence_do_not_trade_when_no_inventory_to_rebalance_with():
    events = [_event(1, "binance", 50.0, 0.01)]  # nothing accumulated yet to sell back
    result = simulate_event_sequence(events, starting_binance_usdt=30.0, starting_bybit_usdt=60.0, binance_floor=25.0, bybit_floor=25.0)
    assert result.steps[0].decision.decision.value == "DO_NOT_TRADE"
    assert result.end_binance_usdt == 30.0  # untouched -- the event never executed
