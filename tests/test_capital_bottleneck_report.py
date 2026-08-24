from datetime import UTC, datetime

import app.reporting.capital_bottleneck_report as report_module
from app.reporting.altcoin_scan_report import AltcoinScanReport, DirectionSummary, OpportunityStatus
from app.reporting.capital_bottleneck_report import build_capital_bottleneck_report, simulate_capital_tier


def _direction(symbol, buy_exchange, net_profit_mean) -> DirectionSummary:
    base = symbol.split("/")[0]
    return DirectionSummary(
        symbol=symbol, buy_exchange=buy_exchange, sell_exchange="bybit" if buy_exchange == "binance" else "binance",
        observations=20, gross_spread_mean_pct=0.5, gross_spread_max_pct=1.0, net_spread_mean_pct=0.3, net_spread_max_pct=0.8,
        net_profit_per_1000usdt_mean=net_profit_mean, net_profit_per_1000usdt_max=net_profit_mean + 1,
        positive_rate_pct=70.0, mean_persistence_seconds=20.0, max_persistence_seconds=40.0,
        unique_detections=5, continuations=5, best_observed_at=datetime(2026, 8, 24, tzinfo=UTC),
        status=OpportunityStatus.STRONG, net_profit_per_1000usdt_median=net_profit_mean,
        net_profit_per_1000usdt_p10=max(0.0, net_profit_mean - 0.5), net_profit_per_1000usdt_min=0.0,
        available_depth_usd_mean=500.0,
    )


# ---- simulate_capital_tier -----------------------------------------------


def test_more_slots_than_candidates_executes_all_of_them():
    candidates = [_direction(f"S{i}/USDT", "binance", 3.0) for i in range(10)]
    result = simulate_capital_tier(candidates, inventory_eligible_base_assets={f"S{i}" for i in range(10)}, total_capital_usdt=160.0, binance_ratio=1.0, bybit_ratio=0.0, max_notional_per_leg_usdt=5.0)
    assert result.executable_profitable_opportunities == 10  # floor(160/5) = 32 slots, but only 10 real candidates exist — can't execute more than that
    assert result.missed_for_capital == 0  # capital was never the constraint here


def test_fewer_slots_than_candidates_caps_execution_and_counts_the_miss():
    candidates = [_direction(f"S{i}/USDT", "binance", 3.0) for i in range(10)]
    result = simulate_capital_tier(candidates, inventory_eligible_base_assets={f"S{i}" for i in range(10)}, total_capital_usdt=20.0, binance_ratio=1.0, bybit_ratio=0.0, max_notional_per_leg_usdt=5.0)
    assert result.executable_profitable_opportunities == 4  # floor(20/5) = 4 slots
    assert result.missed_for_capital == 6  # 10 candidates - 4 slots


def test_best_candidates_prioritized_by_net_profit():
    candidates = [
        _direction("LOW/USDT", "binance", 1.0),
        _direction("HIGH/USDT", "binance", 9.0),
        _direction("MID/USDT", "binance", 5.0),
    ]
    result = simulate_capital_tier(candidates, inventory_eligible_base_assets={"LOW", "HIGH", "MID"}, total_capital_usdt=10.0, binance_ratio=1.0, bybit_ratio=0.0, max_notional_per_leg_usdt=5.0)
    # only 2 slots (floor(10/5)=2) -> HIGH and MID should be chosen over LOW
    assert result.executable_profitable_opportunities == 2
    assert result.simulated_net_pnl_usd == (9.0 + 5.0) * (5.0 / 1000.0)


def test_binance_and_bybit_slots_independent():
    candidates = [_direction("A/USDT", "binance", 3.0), _direction("B/USDT", "bybit", 3.0)]
    result = simulate_capital_tier(candidates, inventory_eligible_base_assets={"A", "B"}, total_capital_usdt=160.0, binance_ratio=100 / 160, bybit_ratio=60 / 160, max_notional_per_leg_usdt=5.0)
    assert result.executable_profitable_opportunities == 2  # both fit easily, one per exchange pool
    assert result.missed_for_capital == 0


def test_missed_for_inventory_counts_executable_but_not_preposition_eligible():
    candidates = [_direction("A/USDT", "binance", 3.0), _direction("B/USDT", "binance", 2.0)]
    result = simulate_capital_tier(candidates, inventory_eligible_base_assets={"A"}, total_capital_usdt=160.0, binance_ratio=1.0, bybit_ratio=0.0, max_notional_per_leg_usdt=5.0)
    assert result.executable_profitable_opportunities == 2
    assert result.missed_for_inventory == 1  # only B is not in the eligible set


def test_zero_candidates_produces_zero_utilization_not_a_crash():
    result = simulate_capital_tier([], inventory_eligible_base_assets=set(), total_capital_usdt=160.0, binance_ratio=0.625, bybit_ratio=0.375, max_notional_per_leg_usdt=5.0)
    assert result.executable_profitable_opportunities == 0
    assert result.capital_utilization_pct == 0.0
    assert result.simulated_net_pnl_usd == 0.0


def test_allocation_split_respects_ratios():
    result = simulate_capital_tier([], inventory_eligible_base_assets=set(), total_capital_usdt=160.0, binance_ratio=100 / 160, bybit_ratio=60 / 160, max_notional_per_leg_usdt=5.0)
    assert result.binance_allocation_usdt == 100.0
    assert result.bybit_allocation_usdt == 60.0


# ---- build_capital_bottleneck_report end-to-end (monkeypatched I/O) ----


def _fake_async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


async def test_report_detects_capital_bottleneck_when_more_candidates_than_slots(monkeypatch):
    scan_report = AltcoinScanReport(
        window_start=None, window_end=None, total_observations=200,
        best_direction_by_symbol=[_direction(f"S{i}/USDT", "binance", 3.0) for i in range(50)],
    )
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(scan_report))

    report = await build_capital_bottleneck_report(session=object())
    assert len(report.tiers) == 4
    assert report.tiers[0].total_capital_usdt == 160.0
    assert report.tiers[-1].total_capital_usdt == 1000.0
    # 50 candidates vastly exceeds available slots at any tier here -> capital is the bottleneck, and more capital should help
    assert report.current_capital_bottleneck is True
    assert report.would_300_materially_help is True


async def test_report_says_capital_does_not_help_when_opportunities_are_the_limit(monkeypatch):
    scan_report = AltcoinScanReport(
        window_start=None, window_end=None, total_observations=10,
        best_direction_by_symbol=[_direction("ONLY/USDT", "binance", 3.0)],  # a single real opportunity, capital is never the constraint
    )
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(scan_report))

    report = await build_capital_bottleneck_report(session=object())
    assert report.current_capital_bottleneck is False
    assert report.would_300_materially_help is False
    assert report.would_500_materially_help is False


async def test_report_never_counts_non_positive_edges_as_candidates(monkeypatch):
    scan_report = AltcoinScanReport(
        window_start=None, window_end=None, total_observations=10,
        best_direction_by_symbol=[_direction("BAD/USDT", "binance", -1.0)],
    )
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(scan_report))

    report = await build_capital_bottleneck_report(session=object())
    assert report.tiers[0].executable_profitable_opportunities == 0
