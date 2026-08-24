import app.reporting.observation_window_report as report_module
from app.execution.inventory_manager import ExchangeInventorySnapshot, InventoryClassification, InventoryManagerReport, InventoryScoreBreakdown
from app.reporting.capital_bottleneck_report import CapitalBottleneckReport
from app.reporting.full_universe_discovery_report import FullMarketDiscoveryReport
from app.reporting.missed_opportunity_report import MissedCauseRow, MissedOpportunityReport
from app.reporting.observation_window_report import ObservationWindowReport, build_observation_window_report, render_observation_window_text
from datetime import UTC, datetime


def _report(**overrides) -> ObservationWindowReport:
    base = dict(
        binance_bybit_opportunities_detected=10, net_positive=3, repeating=1, executable_with_current_inventory=0,
        missed_profitable=7, primary_missed_reason="FEES", best_current_symbol="ZRO/USDT",
        current_capital_bottleneck=False, would_300_materially_help=False, would_300_evidence="capital was not the constraint",
        would_500_materially_help=False, would_500_evidence="capital was not the constraint", inventory_candidate=None,
    )
    base.update(overrides)
    return ObservationWindowReport(**base)


# ---- render_observation_window_text -------------------------------------


def test_render_includes_all_required_lines():
    text = render_observation_window_text(_report())
    for label in [
        "BINANCE↔BYBIT OPPORTUNITIES DETECTED = 10", "NET POSITIVE = 3", "REPEATING = 1",
        "EXECUTABLE WITH CURRENT INVENTORY = 0", "MISSED PROFITABLE = 7", "PRIMARY MISSED REASON = FEES",
        "BEST CURRENT SYMBOL = ZRO/USDT", "CURRENT CAPITAL BOTTLENECK = NO",
        "WOULD 300 USDT MATERIALLY HELP = NO", "WOULD 500 USDT MATERIALLY HELP = NO", "INVENTORY CANDIDATE = NONE",
    ]:
        assert label in text, f"missing: {label}"


def test_render_none_when_nothing_qualifies():
    text = render_observation_window_text(_report(primary_missed_reason=None, best_current_symbol=None, inventory_candidate=None))
    assert "PRIMARY MISSED REASON = NONE" in text
    assert "BEST CURRENT SYMBOL = NONE" in text
    assert "INVENTORY CANDIDATE = NONE" in text


def test_render_shows_inventory_candidate_when_present():
    text = render_observation_window_text(_report(inventory_candidate="LUNC"))
    assert "INVENTORY CANDIDATE = LUNC" in text


# ---- build_observation_window_report composition (monkeypatched I/O) ---


def _fake_async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


def _score(base_asset, classification, total_score=60.0) -> InventoryScoreBreakdown:
    return InventoryScoreBreakdown(
        symbol=f"{base_asset}/USDT", base_asset=base_asset, observations=20, sightings=8, net_positive_rate_pct=70.0,
        median_net_edge_per_1000usdt=3.0, p10_net_edge_per_1000usdt=1.0, frequency_score=0.8, net_edge_score=1.0,
        persistence_score=1.0, liquidity_score=1.0, volatility_score=0.5, expected_reuse_score=0.9, total_score=total_score,
        expected_reuse_label="MEDIUM", expected_additional_executable_trades=8, classification=classification, reason="test",
    )


async def test_best_symbol_omitted_when_top_opportunity_is_not_net_positive(monkeypatch):
    from app.reporting.altcoin_scan_report import DirectionSummary, OpportunityStatus

    discovery = FullMarketDiscoveryReport(
        common_pairs=239, pairs_fast_scanned=239, pairs_deep_validated=10, pairs_raw_spread_stage_a=5,
        pairs_net_positive_stage_b_live=0, pairs_with_repeating_net_edge=0,
        top_10_opportunities=[
            DirectionSummary(
                symbol="BAD/USDT", buy_exchange="binance", sell_exchange="bybit", observations=20,
                gross_spread_mean_pct=0.1, gross_spread_max_pct=0.2, net_spread_mean_pct=0.0, net_spread_max_pct=0.0,
                net_profit_per_1000usdt_mean=-1.0, net_profit_per_1000usdt_max=-0.5, positive_rate_pct=0.0,
                mean_persistence_seconds=0.0, max_persistence_seconds=0.0, unique_detections=0, continuations=0,
                best_observed_at=datetime(2026, 8, 24, tzinfo=UTC), status=OpportunityStatus.NO_EDGE,
            )
        ],
    )
    monkeypatch.setattr(report_module, "build_full_market_discovery_report", _fake_async(discovery))
    monkeypatch.setattr(report_module, "build_missed_opportunity_report", _fake_async(MissedOpportunityReport(causes=[], total_missed=0, total_theoretical_profit_usd=0.0, primary_cause=None)))
    monkeypatch.setattr(report_module, "build_capital_bottleneck_report", _fake_async(CapitalBottleneckReport()))
    inventory = InventoryManagerReport(
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        binance=ExchangeInventorySnapshot(exchange="binance", usdt_available=100.0),
        bybit=ExchangeInventorySnapshot(exchange="bybit", usdt_available=60.0),
        total_usdt_available=160.0, capital_locked_in_inventory_usdt=0.0, prepositioned_assets=[],
        inventory_missing=[], inventory_scores=[], rebalance_candidates=[],
        inventory_pnl_usd=None, inventory_pnl_note="n/a", simulation_only=True,
    )
    monkeypatch.setattr(report_module, "build_inventory_report", _fake_async(inventory))

    report = await build_observation_window_report(session=object())
    assert report.best_current_symbol is None  # negative-edge top result must never be reported as "best"


async def test_inventory_candidate_picks_highest_score_among_eligible(monkeypatch):
    discovery = FullMarketDiscoveryReport(
        common_pairs=239, pairs_fast_scanned=239, pairs_deep_validated=10, pairs_raw_spread_stage_a=5,
        pairs_net_positive_stage_b_live=2, pairs_with_repeating_net_edge=1, top_10_opportunities=[],
    )
    monkeypatch.setattr(report_module, "build_full_market_discovery_report", _fake_async(discovery))
    monkeypatch.setattr(report_module, "build_missed_opportunity_report", _fake_async(MissedOpportunityReport(causes=[MissedCauseRow("FEES", 5, 0.0)], total_missed=5, total_theoretical_profit_usd=0.0, primary_cause="FEES")))
    monkeypatch.setattr(report_module, "build_capital_bottleneck_report", _fake_async(CapitalBottleneckReport()))
    inventory = InventoryManagerReport(
        generated_at=datetime(2026, 8, 24, tzinfo=UTC),
        binance=ExchangeInventorySnapshot(exchange="binance", usdt_available=100.0),
        bybit=ExchangeInventorySnapshot(exchange="bybit", usdt_available=60.0),
        total_usdt_available=160.0, capital_locked_in_inventory_usdt=0.0, prepositioned_assets=["ZRO"],
        inventory_missing=[], inventory_scores=[
            _score("WEAK", InventoryClassification.DO_NOT_PREPOSITION, total_score=10.0),
            _score("BEST", InventoryClassification.STRONG_PREPOSITION_CANDIDATE, total_score=90.0),
            _score("OK", InventoryClassification.PREPOSITION_CANDIDATE, total_score=55.0),
        ], rebalance_candidates=[],
        inventory_pnl_usd=None, inventory_pnl_note="n/a", simulation_only=True,
    )
    monkeypatch.setattr(report_module, "build_inventory_report", _fake_async(inventory))

    report = await build_observation_window_report(session=object())
    assert report.inventory_candidate == "BEST"
    assert report.executable_with_current_inventory == 1  # len(prepositioned_assets)
    assert report.missed_profitable == 5
    assert report.primary_missed_reason == "FEES"
