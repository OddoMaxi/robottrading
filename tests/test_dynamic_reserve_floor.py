from app.execution.dynamic_reserve_floor import compute_dynamic_reserve_floor


def _base_kwargs(**overrides):
    kwargs = dict(
        exchange="okx",
        max_notional_per_leg_usdt=10.0,
        min_notional_usdt=5.0,
        recent_buy_count=1,
        recent_sell_count=1,
        total_capital_usdt=150.0,
        num_active_exchanges=3,
    )
    kwargs.update(overrides)
    return kwargs


def test_balanced_activity_lands_on_operating_buffer():
    result = compute_dynamic_reserve_floor(**_base_kwargs())
    # imbalance_factor = 1/1 = 1.0 -> operating_buffer = 2.5 * 10 * 1.0 = 25.0
    # structural_minimum = 2.0 * 5.0 = 10.0
    # equal_share_cap = (150/3) * 0.6 = 30.0
    assert result.structural_minimum_usdt == 10.0
    assert result.operating_buffer_usdt == 25.0
    assert result.equal_share_cap_usdt == 30.0
    assert result.floor_usdt == 25.0
    assert result.binding_component == "OPERATING_BUFFER"


def test_matches_fixed_25_at_deployed_defaults_when_balanced():
    result = compute_dynamic_reserve_floor(**_base_kwargs())
    assert result.floor_usdt == 25.0  # same as capital_rebalancer.compute_reserve_floor(10.0)


def test_buy_heavy_exchange_gets_a_higher_floor_than_balanced():
    # total_capital raised well above the balanced case so the equal-share cap
    # doesn't mask the operating-buffer effect this test targets.
    balanced = compute_dynamic_reserve_floor(**_base_kwargs(total_capital_usdt=1000.0))
    buy_heavy = compute_dynamic_reserve_floor(**_base_kwargs(recent_buy_count=8, recent_sell_count=1, total_capital_usdt=1000.0))
    assert buy_heavy.floor_usdt > balanced.floor_usdt
    assert buy_heavy.imbalance_factor == 5.0  # clamped at imbalance_factor_cap
    assert buy_heavy.binding_component == "OPERATING_BUFFER"


def test_sell_heavy_exchange_floor_never_drops_below_one_x_multiplier():
    sell_heavy = compute_dynamic_reserve_floor(**_base_kwargs(recent_buy_count=0, recent_sell_count=9))
    balanced = compute_dynamic_reserve_floor(**_base_kwargs())
    # imbalance_factor floors at 1.0 even with zero recent buys (a reversal is always possible)
    assert sell_heavy.imbalance_factor == 1.0
    assert sell_heavy.floor_usdt == balanced.floor_usdt


def test_structural_minimum_binds_when_operating_buffer_is_tiny():
    result = compute_dynamic_reserve_floor(**_base_kwargs(
        max_notional_per_leg_usdt=1.0, min_notional_usdt=20.0, recent_buy_count=1, recent_sell_count=1,
    ))
    # structural_minimum = 2.0 * 20.0 = 40.0; operating_buffer = 2.5 * 1.0 * 1.0 = 2.5
    assert result.structural_minimum_usdt == 40.0
    assert result.floor_usdt == 40.0
    assert result.binding_component == "STRUCTURAL_MINIMUM"


def test_equal_share_cap_binds_when_operating_buffer_would_starve_other_exchanges():
    result = compute_dynamic_reserve_floor(**_base_kwargs(
        max_notional_per_leg_usdt=50.0, recent_buy_count=9, recent_sell_count=1,
        total_capital_usdt=60.0, num_active_exchanges=3,
    ))
    # operating_buffer = 2.5 * 50 * 5.0(capped) = 625.0
    # equal_share_cap = (60/3) * 0.6 = 12.0, but structural_minimum = 10.0, so cap floor at max(12.0, 10.0) = 12.0
    assert result.operating_buffer_usdt == 625.0
    assert result.equal_share_cap_usdt == 12.0
    assert result.floor_usdt == 12.0
    assert result.binding_component == "EQUAL_SHARE_CAP"


def test_single_active_exchange_gets_the_full_equal_share():
    result = compute_dynamic_reserve_floor(**_base_kwargs(num_active_exchanges=1, total_capital_usdt=100.0))
    assert result.equal_share_cap_usdt == 60.0


def test_zero_recent_sell_count_does_not_divide_by_zero():
    result = compute_dynamic_reserve_floor(**_base_kwargs(recent_buy_count=3, recent_sell_count=0))
    assert result.imbalance_factor == 3.0  # 3 / max(1, 0) = 3.0


def test_pure_function_same_inputs_same_output():
    kwargs = _base_kwargs(recent_buy_count=4, recent_sell_count=2)
    assert compute_dynamic_reserve_floor(**kwargs) == compute_dynamic_reserve_floor(**kwargs)


def test_result_exposes_exchange_name_unchanged():
    result = compute_dynamic_reserve_floor(**_base_kwargs(exchange="binance"))
    assert result.exchange == "binance"
