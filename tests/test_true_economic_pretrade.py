import pytest

from app.execution.true_economic_ledger import apply_buy, empty_pool, get_pool, seed_pool
from app.execution.true_economic_pretrade import (
    evaluate_arbitrage_true_economics,
    evaluate_inventory_constitution_true_economics,
    simulate_rebalance,
)


def test_rvn_first_v4_cycle_would_be_rejected_by_v5():
    """Real fill data, first V4 arbitrage cycle. V4's own formula reported
    +0.209185 and executed the trade; V5's true-economic gate must refuse
    it, because the RVN actually sold from Bybit's pool had a real cost
    basis (0.003416/unit, session-start) above what it fetched."""
    sell_pool = get_pool(seed_pool({}, "bybit", "RVN", qty=2200.8922, price=0.003416), "bybit", "RVN")
    buy_pool = empty_pool("binance", "RVN")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=2130.9, sell_price=0.003415, sell_fee_amount=0.0072770235, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=2133.1, buy_price=0.00331, buy_fee_amount=2.1331, buy_fee_asset="RVN",
        buy_side_mark_price=0.00331,  # no independent bid supplied historically -- use the fill price itself as the mark
        required_safety_margin_usd=0.0,
    )
    assert quote.sell_side_realized_pnl_usd == pytest.approx(-0.009407923500000415, abs=1e-9)
    assert quote.would_trade is False


def test_zil_real_fill_true_economics():
    """Real ZIL fill data (2026-08-25T09:29:39Z cycle); pool seeded with a
    documented, round representative starting position -- not a claim of
    the exact mid-session state (which had already absorbed many earlier
    buys/sells by this point in the real session)."""
    sell_pool = get_pool(seed_pool({}, "bybit", "ZIL", qty=5000.0, price=0.00280), "bybit", "ZIL")
    buy_pool = empty_pool("binance", "ZIL")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=2612.1, sell_price=0.002818, sell_fee_amount=0.0073608978, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=2614.8, buy_price=0.002775, buy_fee_amount=2.6148, buy_fee_asset="ZIL",
        buy_side_mark_price=0.002775, required_safety_margin_usd=0.0,
    )
    assert quote.sell_side_realized_pnl_usd == pytest.approx(0.03965690220000084, abs=1e-9)


def test_lunc_real_fill_true_economics():
    """Real LUNC fill data (2026-08-25T09:14:52Z cycle), documented seed."""
    sell_pool = get_pool(seed_pool({}, "bybit", "LUNC", qty=200000.0, price=0.0000548), "bybit", "LUNC")
    buy_pool = empty_pool("binance", "LUNC")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=134828.0, sell_price=0.00005512, sell_fee_amount=0.00743171936, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=134963.0, buy_price=0.00005477, buy_fee_amount=134.963, buy_fee_asset="LUNC",
        buy_side_mark_price=0.00005477, required_safety_margin_usd=0.0,
    )
    assert quote.sell_side_realized_pnl_usd == pytest.approx(0.03571324063999981, abs=1e-9)


def test_sand_real_fill_true_economics_reversed_direction():
    """Real SAND fill data (2026-08-25T08:47:47Z cycle) -- this cycle ran
    buy-on-bybit/sell-on-binance, the opposite direction from the RVN/ZIL/
    LUNC cases above, exercising that the engine has no hardcoded
    exchange-direction assumption."""
    sell_pool = get_pool(seed_pool({}, "binance", "SAND", qty=300.0, price=0.04260), "binance", "SAND")
    buy_pool = empty_pool("bybit", "SAND")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=171.0, sell_price=0.04285, sell_fee_amount=0.00732735, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=172.0, buy_price=0.04246, buy_fee_amount=0.172, buy_fee_asset="SAND",
        buy_side_mark_price=0.04246, required_safety_margin_usd=0.0,
    )
    assert quote.sell_side_realized_pnl_usd == pytest.approx(0.03542265000000011, abs=1e-9)


def test_insufficient_sell_inventory_refuses_the_trade_never_fabricates_a_cost_basis():
    sell_pool = empty_pool("bybit", "RVN")
    buy_pool = empty_pool("binance", "RVN")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=1000.0, sell_price=0.0035, sell_fee_amount=0.0, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=1000.0, buy_price=0.0033, buy_fee_amount=0.0, buy_fee_asset="USDT",
        buy_side_mark_price=0.0033,
    )
    assert quote.would_trade is False
    assert quote.sell_side_realized_pnl_usd is None
    assert quote.expected_true_wealth_delta_usd is None
    assert "SELL_COST_BASIS_UNKNOWN" in quote.reason


def test_mark_to_market_captures_spread_cost_on_the_fresh_buy():
    """A fresh buy crosses the ask; marking it immediately at the (lower)
    bid captures the real, instant cost of doing so -- a positive-looking
    same-cycle notional match can hide this entirely."""
    sell_pool = get_pool(seed_pool({}, "bybit", "RVN", qty=10_000.0, price=0.0030), "bybit", "RVN")
    buy_pool = empty_pool("binance", "RVN")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=1000.0, sell_price=0.0032, sell_fee_amount=0.0, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=1000.0, buy_price=0.0031, buy_fee_amount=0.0, buy_fee_asset="USDT",
        buy_side_mark_price=0.00305,  # real bid on the buy exchange, below the ask just paid
        required_safety_margin_usd=0.0,
    )
    assert quote.expected_buy_inventory_delta_usd == pytest.approx(1000.0 * (0.00305 - 0.0031))
    assert quote.expected_buy_inventory_delta_usd < 0
    # sell side alone looks profitable (0.0032 > 0.0030 cost basis) but the
    # buy-side spread cost must still be counted in the total
    assert quote.sell_side_realized_pnl_usd == pytest.approx(1000.0 * (0.0032 - 0.0030))
    assert quote.expected_true_wealth_delta_usd == pytest.approx(
        quote.sell_side_realized_pnl_usd + quote.expected_buy_inventory_delta_usd
    )


def test_rebalance_before_arbitrage_can_flip_a_nominal_profit_negative():
    """Item 3, user directive: "une opportunite apparemment a +0.20 USDT
    mais necessitant -0.25 USDT de rebalance doit devenir TRUE ECONOMIC
    OPPORTUNITY = NEGATIVE -> NO TRADE." The rebalance itself is
    pre-simulated against its own real pool (not assumed neutral, not
    using the old non-depleting cost-basis function)."""
    rebalance_pool = get_pool(seed_pool({}, "binance", "ZIL", qty=1000.0, price=0.0030), "binance", "ZIL")
    rebalance_sim = simulate_rebalance(rebalance_pool, qty_to_sell=500.0, price=0.0020, fee_amount=0.0, fee_asset="USDT")
    assert rebalance_sim is not None
    assert rebalance_sim.realized_pnl_usd == pytest.approx(500.0 * (0.0020 - 0.0030), abs=1e-9)  # -0.50, a real cost

    # arbitrage leg alone: sell at 0.0032 against a 0.0030 cost basis, buy
    # and mark at the same 0.0032 (zero spread cost on the buy side) ->
    # +0.20 USDT, matching the user's own "+0.20 USDT" example exactly.
    sell_pool = get_pool(seed_pool({}, "bybit", "RVN", qty=10_000.0, price=0.0030), "bybit", "RVN")
    buy_pool = empty_pool("binance", "RVN")
    quote_without_rebalance = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=1000.0, sell_price=0.00320, sell_fee_amount=0.0, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=1000.0, buy_price=0.00320, buy_fee_amount=0.0, buy_fee_asset="USDT",
        buy_side_mark_price=0.00320, required_safety_margin_usd=0.0,
    )
    assert quote_without_rebalance.would_trade is True
    assert quote_without_rebalance.expected_true_wealth_delta_usd == pytest.approx(0.20, abs=1e-9)

    # -0.50 USDT of rebalance cost on top of a +0.20 USDT edge -> -0.30 USDT net -> NO TRADE
    quote_with_rebalance = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=1000.0, sell_price=0.00320, sell_fee_amount=0.0, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=1000.0, buy_price=0.00320, buy_fee_amount=0.0, buy_fee_asset="USDT",
        buy_side_mark_price=0.00320, required_safety_margin_usd=0.0,
        rebalance_impact_usd=rebalance_sim.realized_pnl_usd,
    )
    assert quote_with_rebalance.expected_true_wealth_delta_usd == pytest.approx(
        quote_without_rebalance.expected_true_wealth_delta_usd + rebalance_sim.realized_pnl_usd
    )
    assert quote_with_rebalance.expected_true_wealth_delta_usd == pytest.approx(-0.30, abs=1e-9)
    assert quote_with_rebalance.would_trade is False


def test_rebalance_simulation_returns_none_when_pool_cannot_cover_it():
    pool = get_pool(seed_pool({}, "binance", "ZIL", qty=100.0, price=0.0030), "binance", "ZIL")
    assert simulate_rebalance(pool, qty_to_sell=500.0, price=0.0025, fee_amount=0.0, fee_asset="USDT") is None


def test_inventory_constitution_is_never_assumed_neutral():
    """Item 4: a fresh buy, valued immediately at the sell-side mark
    price, has a real (typically negative) wealth effect from crossing
    the spread -- never a free/neutral action by default."""
    pool = empty_pool("bybit", "RVN")
    quote = evaluate_inventory_constitution_true_economics(
        pool, qty=1000.0, ask_price=0.00331, mark_price=0.00329, fee_amount=1.0, fee_asset="RVN",
    )
    assert quote.wealth_delta_usd < 0
    assert quote.would_constitute is False


def test_inventory_constitution_blocks_repeated_buys_into_an_underwater_pool():
    """Item 4: "interdire les recyclages repetitifs qui produisent un faux
    edge tout en accumulant du stock economiquement defavorable" --
    operationalized as the same true-economic gate applied uniformly. A
    pool already averaged well above the current mark must not keep
    absorbing more buys at a price that doesn't clear the margin."""
    pool = get_pool(seed_pool({}, "bybit", "RVN", qty=1000.0, price=0.0040), "bybit", "RVN")  # already priced well above current market
    quote = evaluate_inventory_constitution_true_economics(
        pool, qty=500.0, ask_price=0.0033, mark_price=0.0032, fee_amount=0.0, fee_asset="USDT",
    )
    assert quote.already_underwater is True
    assert quote.would_constitute is False


def test_inventory_constitution_allows_a_favorable_buy_even_into_an_underwater_pool():
    """A buy priced BELOW the pool's damaged average, itself still
    clearing the margin against the mark, must not be blocked just
    because the pool average is currently bad -- would_constitute follows
    the wealth-delta gate, never a blanket "pool is underwater" veto."""
    pool = get_pool(seed_pool({}, "bybit", "RVN", qty=1000.0, price=0.0040), "bybit", "RVN")
    quote = evaluate_inventory_constitution_true_economics(
        pool, qty=500.0, ask_price=0.0030, mark_price=0.0032, fee_amount=0.0, fee_asset="USDT",
    )
    assert quote.already_underwater is True
    assert quote.would_constitute is True


def test_fees_are_reported_but_not_double_subtracted():
    sell_pool = get_pool(seed_pool({}, "bybit", "RVN", qty=10_000.0, price=0.0030), "bybit", "RVN")
    buy_pool = empty_pool("binance", "RVN")
    quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=1000.0, sell_price=0.0035, sell_fee_amount=0.35, sell_fee_asset="USDT",
        buy_pool=buy_pool, buy_qty=1000.0, buy_price=0.0030, buy_fee_amount=0.30, buy_fee_asset="USDT",
        buy_side_mark_price=0.0030, required_safety_margin_usd=0.0,
    )
    assert quote.expected_total_fees_usd == pytest.approx(0.65)
    # the wealth delta already has both fees netted in via realized_pnl and new_buy_cost --
    # it must not equal a naive (proceeds - cost) that ignores fees, nor double-subtract them
    naive_gross = 1000.0 * (0.0035 - 0.0030)
    assert quote.expected_true_wealth_delta_usd == pytest.approx(naive_gross - 0.65)
