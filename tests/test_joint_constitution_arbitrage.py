import pytest

from app.execution.true_economic_ledger import empty_pool, get_pool, seed_pool
from app.execution.true_economic_pretrade import evaluate_inventory_constitution_true_economics, evaluate_joint_constitution_and_arbitrage


def test_same_moment_full_constitution_from_zero_is_always_a_wash_or_worse():
    """Mathematical property, not a bug: when the ENTIRE quantity sold is
    constituted in the same moment it's sold, SELL_SIDE_REALIZED_PNL
    reduces to exactly -(sell exchange's own bid-ask spread) and
    BUY_SIDE_MARK_TO_MARKET_DELTA reduces to exactly -(buy exchange's own
    bid-ask spread), regardless of the apparent cross-exchange edge size
    -- the classic buy_ask-vs-sell_bid comparison never actually enters
    the calculation in this framing, because the sell leg is priced
    against ITS OWN just-set cost basis (the constitution's own ask), not
    against the buy exchange at all. This holds even for a large (193bps)
    apparent cross-exchange edge -- proving the isolated ask-then-bid gate
    and this joint one agree exactly when there is no pre-existing
    inventory to blend with: both correctly refuse a same-instant round
    trip that crosses two exchanges' spreads for a single edge."""
    sell_pool = empty_pool("bybit", "XYZ")
    quote = evaluate_joint_constitution_and_arbitrage(
        sell_pool=sell_pool,
        constitution_qty=100.0, constitution_price=1.00, constitution_fee_amount=0.0, constitution_fee_asset="USDT",
        arb_sell_qty=100.0, arb_sell_price=0.999, arb_sell_fee_amount=0.0, arb_sell_fee_asset="USDT",  # bybit's own bid, 10bps below its ask
        buy_pool=empty_pool("binance", "XYZ"), arb_buy_qty=100.0, arb_buy_price=0.98, arb_buy_fee_amount=0.0, arb_buy_fee_asset="USDT",  # a large, genuine-looking 193bps cross-exchange edge vs bybit's bid
        arb_buy_side_mark_price=0.979,  # binance's own bid, 10bps below its ask
    )
    assert quote.would_proceed is False
    # -(0.001) sell-side spread cost + -(0.001) buy-side spread cost, times qty=100
    assert quote.total_true_economic_result_usd == pytest.approx(-0.2, abs=1e-6)


def test_isolated_and_joint_agree_when_starting_from_zero_inventory():
    """Confirms the isolated gate (evaluate_inventory_constitution_true_
    economics) and the joint one reach the SAME (reject) verdict when
    there is no pre-existing inventory to blend with -- the isolated
    gate is not "wrong" in this case, it's actually representative."""
    pool = empty_pool("bybit", "XYZ")
    isolated = evaluate_inventory_constitution_true_economics(
        pool, qty=100.0, ask_price=1.00, mark_price=0.999, fee_amount=0.0, fee_asset="USDT",
    )
    joint = evaluate_joint_constitution_and_arbitrage(
        sell_pool=pool,
        constitution_qty=100.0, constitution_price=1.00, constitution_fee_amount=0.0, constitution_fee_asset="USDT",
        arb_sell_qty=100.0, arb_sell_price=0.999, arb_sell_fee_amount=0.0, arb_sell_fee_asset="USDT",
        buy_pool=empty_pool("binance", "XYZ"), arb_buy_qty=100.0, arb_buy_price=0.98, arb_buy_fee_amount=0.0, arb_buy_fee_asset="USDT",
        arb_buy_side_mark_price=0.979,
    )
    assert isolated.would_constitute is False
    assert joint.would_proceed is False


def test_isolated_constitution_would_have_rejected_but_joint_accepts_a_realistic_topup():
    """The scenario the mission actually describes: the sell exchange
    already holds SOME cheap inventory from earlier (0.90/unit, real
    historical cost basis) but not quite enough for the desired trade
    size -- constitution tops up only the shortfall at TODAY's higher
    ask. Judged in isolation, that small top-up alone shows a paper loss
    (bought at today's ask, marked at today's bid) and the isolated gate
    correctly flags it as such. Judged jointly -- what the arbitrage
    actually sells is the BLENDED pool, overwhelmingly the old cheap
    units -- the combined operation is genuinely, substantially
    profitable. This is the real mechanism the mission's item 5 is
    pointing at: "cela bloque structurellement certains arbitrages
    valables" refers to blocking a valid TOP-UP, not to same-moment
    constitution from zero (which is correctly never profitable, see
    the tests above)."""
    pool = get_pool(seed_pool({}, "bybit", "XYZ", qty=90.0, price=0.90), "bybit", "XYZ")

    isolated_topup = evaluate_inventory_constitution_true_economics(
        pool, qty=10.0, ask_price=1.00, mark_price=0.999, fee_amount=0.0, fee_asset="USDT",
    )
    assert isolated_topup.would_constitute is False  # confirms the structural block described in the mission

    joint = evaluate_joint_constitution_and_arbitrage(
        sell_pool=pool,
        constitution_qty=10.0, constitution_price=1.00, constitution_fee_amount=0.0, constitution_fee_asset="USDT",
        arb_sell_qty=100.0, arb_sell_price=0.999, arb_sell_fee_amount=0.0, arb_sell_fee_asset="USDT",
        buy_pool=empty_pool("binance", "XYZ"), arb_buy_qty=100.0, arb_buy_price=0.98, arb_buy_fee_amount=0.0, arb_buy_fee_asset="USDT",
        arb_buy_side_mark_price=0.979,
    )
    assert joint.would_proceed is True
    assert joint.total_true_economic_result_usd > 0
    # sanity: realized pnl on the blended pool should dominate and be strongly positive
    assert joint.arbitrage.sell_side_realized_pnl_usd > 5.0


def test_residual_inventory_valued_honestly_not_fabricated():
    # Constitute slightly more than the arbitrage sells (a real margin, e.g. compute_required_inventory_qty's buffer).
    sell_pool = empty_pool("bybit", "XYZ")
    quote = evaluate_joint_constitution_and_arbitrage(
        sell_pool=sell_pool,
        constitution_qty=105.0, constitution_price=1.00, constitution_fee_amount=0.105, constitution_fee_asset="USDT",
        arb_sell_qty=100.0, arb_sell_price=1.00, arb_sell_fee_amount=0.10, arb_sell_fee_asset="USDT",
        buy_pool=empty_pool("binance", "XYZ"), arb_buy_qty=100.0, arb_buy_price=0.95, arb_buy_fee_amount=0.095, arb_buy_fee_asset="USDT",
        arb_buy_side_mark_price=0.95, residual_mark_price=1.02,
    )
    assert quote.residual_qty == pytest.approx(5.0, abs=1e-6)
    assert quote.residual_value_usd is not None


def test_no_residual_value_computed_without_a_mark_price():
    sell_pool = empty_pool("bybit", "XYZ")
    quote = evaluate_joint_constitution_and_arbitrage(
        sell_pool=sell_pool,
        constitution_qty=105.0, constitution_price=1.00, constitution_fee_amount=0.0, constitution_fee_asset="USDT",
        arb_sell_qty=100.0, arb_sell_price=1.00, arb_sell_fee_amount=0.0, arb_sell_fee_asset="USDT",
        buy_pool=empty_pool("binance", "XYZ"), arb_buy_qty=100.0, arb_buy_price=0.95, arb_buy_fee_amount=0.0, arb_buy_fee_asset="USDT",
        arb_buy_side_mark_price=0.95,
    )
    assert quote.residual_value_usd is None  # never fabricated when no price was supplied
    assert quote.residual_qty == 5.0
