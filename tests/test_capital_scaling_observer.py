import uuid

from app.execution.capital_scaling_observer import OBSERVATION_TIERS_USDT, observe_at_tiers
from app.execution.dual_leg_quote import LegSnapshot

OPP_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


def _leg(exchange: str, side: str, price: float, depth_usd: float, *, taker_fee_rate=0.001, min_notional=5.0) -> LegSnapshot:
    """Deep, uniform depth at a single price level totaling `depth_usd`
    -- enough for tests that don't care about slippage shape."""
    qty_at_price = depth_usd / price
    return LegSnapshot(
        exchange=exchange, side=side, best_bid=price, best_ask=price, depth_levels=[(price, qty_at_price)],
        min_qty=0.0001, step_size=0.0001, tick_size=0.00001, min_notional=min_notional, tradable=True,
        maker_fee_rate=None, taker_fee_rate=taker_fee_rate, fee_source="real_account_fee",
        fetch_started_at=1000.0, fetch_completed_at=1000.0,
    )


def test_observes_every_tier_by_default():
    buy_leg = _leg("binance", "buy", 1.0, depth_usd=100_000)
    sell_leg = _leg("bybit", "sell", 1.01, depth_usd=100_000)
    observations = observe_at_tiers(symbol="RVN/USDT", buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=OPP_ID)
    assert [o.tier_usdt for o in observations] == list(OBSERVATION_TIERS_USDT)


def test_deep_liquid_market_is_executable_and_depth_sufficient_at_every_tier():
    buy_leg = _leg("binance", "buy", 1.0, depth_usd=1_000_000)
    sell_leg = _leg("bybit", "sell", 1.02, depth_usd=1_000_000)  # 2% gross spread, comfortably covers fees/slippage
    observations = observe_at_tiers(symbol="RVN/USDT", buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=OPP_ID)
    assert all(o.depth_sufficient for o in observations)
    assert all(o.net_profit_usd > 0 for o in observations)
    assert all(o.executable for o in observations)


def test_shallow_depth_fails_larger_tiers_but_not_smaller_ones():
    """Only ~60 USDT of real depth exists on the thin side -- the 10/20/50
    tiers should still find enough depth, the 100+ tiers should not."""
    buy_leg = _leg("binance", "buy", 1.0, depth_usd=1_000_000)
    sell_leg = _leg("bybit", "sell", 1.02, depth_usd=60.0)  # the constraining, thin side
    observations = observe_at_tiers(symbol="RVN/USDT", buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=OPP_ID)
    by_tier = {o.tier_usdt: o for o in observations}
    assert by_tier[10.0].depth_sufficient is True
    assert by_tier[20.0].depth_sufficient is True
    assert by_tier[1000.0].depth_sufficient is False


def test_net_profit_and_fees_scale_roughly_with_size_in_a_deep_market():
    buy_leg = _leg("binance", "buy", 1.0, depth_usd=1_000_000)
    sell_leg = _leg("bybit", "sell", 1.02, depth_usd=1_000_000)
    observations = observe_at_tiers(symbol="RVN/USDT", buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=OPP_ID)
    by_tier = {o.tier_usdt: o for o in observations}
    assert by_tier[1000.0].net_profit_usd > by_tier[10.0].net_profit_usd
    assert by_tier[1000.0].total_fees_usd > by_tier[10.0].total_fees_usd


def test_below_min_notional_tier_is_not_executable():
    buy_leg = _leg("binance", "buy", 1.0, depth_usd=100_000, min_notional=15.0)
    sell_leg = _leg("bybit", "sell", 1.02, depth_usd=100_000, min_notional=15.0)
    observations = observe_at_tiers(symbol="RVN/USDT", buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=OPP_ID)
    by_tier = {o.tier_usdt: o for o in observations}
    assert by_tier[10.0].executable is False
    assert "min_notional" in (by_tier[10.0].reason or "")
    assert by_tier[20.0].executable is True


def test_never_places_an_order_or_touches_settings():
    """Structural check: this module imports nothing execution/settings
    related beyond the pure dual_leg_quote machinery."""
    import ast
    from pathlib import Path

    source = Path("app/execution/capital_scaling_observer.py").read_text()
    tree = ast.parse(source)
    imported = {alias.name for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) for alias in node.names}
    assert "place_market_order" not in imported
    assert "get_settings" not in imported


def test_custom_tiers_can_be_supplied():
    buy_leg = _leg("binance", "buy", 1.0, depth_usd=100_000)
    sell_leg = _leg("bybit", "sell", 1.02, depth_usd=100_000)
    observations = observe_at_tiers(symbol="RVN/USDT", buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=OPP_ID, tiers_usdt=(5.0, 15.0))
    assert [o.tier_usdt for o in observations] == [5.0, 15.0]
