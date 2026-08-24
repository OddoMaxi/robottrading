import app.reporting.inventory_manager_v2_report as v2_module
from app.execution.inventory_manager import ExchangeInventorySnapshot, InventoryManagerReport, RebalanceRecommendation
from app.reporting.full_universe_discovery_report import FullMarketDiscoveryReport
from app.reporting.inventory_manager_v2_report import InventoryManagerV2FinalReport, build_inventory_manager_v2_report, render_v2_report_text
from datetime import UTC, datetime


def _rec(action="BUY_INVENTORY", exchange="bybit", asset="ZRO", notional=5.0, classification="PREPOSITION_CANDIDATE") -> RebalanceRecommendation:
    return RebalanceRecommendation(
        action=action, exchange=exchange, asset=asset, recommended_notional_usdt=notional,
        current_holding_usdt_equiv=0.0, capital_required_usdt=notional, inventory_score=60.0,
        classification=classification, sightings=5, net_positive_rate_pct=80.0, median_net_edge=3.0,
        p10_net_edge=1.0, expected_reuse_label="MEDIUM", expected_additional_executable_trades=5,
        reason="test reason", simulated=True,
    )


def _v2_report(**overrides) -> InventoryManagerV2FinalReport:
    base = dict(
        common_universe=239, pairs_actually_scanned=239, pairs_raw_spread_stage_a=10,
        pairs_net_positive_stage_b_live=3, pairs_with_repeating_net_edge=1,
        top_10_symbols=["ZRO/USDT binance→bybit (3.00/1000usdt)"],
        strong_inventory_candidates=["LUNC"],
        recommended_bybit_inventory=[_rec()],
        recommended_binance_inventory=[],
        total_recommended_capital_locked_usdt=5.0,
        real_inventory_orders=0,
        ready_to_enable_automatic_real_inventory_management=False,
        ready_reason="simulation-only build",
    )
    base.update(overrides)
    return InventoryManagerV2FinalReport(**base)


# ---- render_v2_report_text ---------------------------------------------


def test_render_includes_all_required_lines():
    text = render_v2_report_text(_v2_report())
    for label in [
        "COMMON UNIVERSE = 239", "PAIRS ACTUALLY SCANNED = 239", "PAIRS WITH RAW EDGE (STAGE A, Binance/Bybit estimate) = 10",
        "PAIRS NET POSITIVE AFTER COSTS (STAGE B, Binance/Bybit real) = 3", "PAIRS WITH REPEATING NET EDGE = 1",
        "TOP 10 SYMBOLS =", "STRONG INVENTORY CANDIDATES = LUNC",
        "RECOMMENDED BYBIT INVENTORY =", "RECOMMENDED BINANCE INVENTORY = NONE / NO ACTION",
        "TOTAL RECOMMENDED CAPITAL LOCKED = 5.00 USDT", "REAL INVENTORY ORDERS = 0",
        "READY TO ENABLE AUTOMATIC REAL INVENTORY MANAGEMENT = NO",
    ]:
        assert label in text, f"missing line: {label}"


def test_render_never_forces_yes():
    text = render_v2_report_text(_v2_report(ready_to_enable_automatic_real_inventory_management=False))
    assert "READY TO ENABLE AUTOMATIC REAL INVENTORY MANAGEMENT = NO" in text
    assert "= YES" not in text


def test_render_no_action_when_nothing_qualifies():
    report = _v2_report(strong_inventory_candidates=[], recommended_bybit_inventory=[], recommended_binance_inventory=[], top_10_symbols=[])
    text = render_v2_report_text(report)
    assert "STRONG INVENTORY CANDIDATES = NONE" in text
    assert "RECOMMENDED BYBIT INVENTORY = NONE / NO ACTION" in text
    assert "RECOMMENDED BINANCE INVENTORY = NONE / NO ACTION" in text
    assert "TOP 10 SYMBOLS = NONE" in text


def test_render_real_inventory_orders_always_zero_in_text():
    text = render_v2_report_text(_v2_report(real_inventory_orders=0))
    assert "REAL INVENTORY ORDERS = 0" in text


# ---- build_inventory_manager_v2_report composition ---------------------


def _fake_async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


async def test_composition_splits_buys_by_exchange_and_sums_capital(monkeypatch):
    discovery = FullMarketDiscoveryReport(
        common_pairs=100, pairs_fast_scanned=100, pairs_deep_validated=20, pairs_raw_spread_stage_a=8,
        pairs_net_positive_stage_b_live=2, pairs_with_repeating_net_edge=1, top_10_opportunities=[],
    )
    inventory = InventoryManagerReport(
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        binance=ExchangeInventorySnapshot(exchange="binance", usdt_available=100.0),
        bybit=ExchangeInventorySnapshot(exchange="bybit", usdt_available=60.0),
        total_usdt_available=160.0, capital_locked_in_inventory_usdt=0.0, prepositioned_assets=[],
        inventory_missing=[], inventory_scores=[],
        rebalance_candidates=[
            _rec(exchange="bybit", asset="ZRO", notional=5.0),
            _rec(exchange="binance", asset="STX", notional=4.0),
            _rec(action="SELL_INVENTORY", exchange="binance_or_bybit", asset="OLD", notional=2.0),
        ],
        inventory_pnl_usd=None, inventory_pnl_note="n/a", simulation_only=True,
    )
    monkeypatch.setattr(v2_module, "build_full_market_discovery_report", _fake_async(discovery))
    monkeypatch.setattr(v2_module, "build_inventory_report", _fake_async(inventory))

    report = await build_inventory_manager_v2_report(session=object())
    assert len(report.recommended_bybit_inventory) == 1
    assert report.recommended_bybit_inventory[0].asset == "ZRO"
    assert len(report.recommended_binance_inventory) == 1
    assert report.recommended_binance_inventory[0].asset == "STX"
    assert report.total_recommended_capital_locked_usdt == 9.0  # only the two BUY_INVENTORY recs, SELL excluded
    assert report.real_inventory_orders == 0
    assert report.ready_to_enable_automatic_real_inventory_management is False


async def test_composition_never_ready_regardless_of_data(monkeypatch):
    """Even a perfect-looking report must never flip
    ready_to_enable_automatic_real_inventory_management — that gate is
    structural (no order path exists), not a data verdict."""
    discovery = FullMarketDiscoveryReport(
        common_pairs=500, pairs_fast_scanned=500, pairs_deep_validated=200, pairs_raw_spread_stage_a=100,
        pairs_net_positive_stage_b_live=50, pairs_with_repeating_net_edge=50, top_10_opportunities=[],
    )
    inventory = InventoryManagerReport(
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        binance=ExchangeInventorySnapshot(exchange="binance", usdt_available=100.0),
        bybit=ExchangeInventorySnapshot(exchange="bybit", usdt_available=60.0),
        total_usdt_available=160.0, capital_locked_in_inventory_usdt=0.0, prepositioned_assets=[],
        inventory_missing=[], inventory_scores=[], rebalance_candidates=[],
        inventory_pnl_usd=None, inventory_pnl_note="n/a", simulation_only=True,
    )
    monkeypatch.setattr(v2_module, "build_full_market_discovery_report", _fake_async(discovery))
    monkeypatch.setattr(v2_module, "build_inventory_report", _fake_async(inventory))

    report = await build_inventory_manager_v2_report(session=object())
    assert report.ready_to_enable_automatic_real_inventory_management is False
