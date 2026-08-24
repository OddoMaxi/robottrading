from datetime import UTC, datetime

import app.reporting.full_universe_discovery_report as report_module
from app.reporting.full_universe_discovery_report import build_full_market_discovery_report
from app.reporting.altcoin_scan_report import AltcoinScanReport, DirectionSummary, OpportunityStatus


def _summary(symbol, net_profit_mean, detections=0, continuations=0) -> DirectionSummary:
    return DirectionSummary(
        symbol=symbol, buy_exchange="binance", sell_exchange="bybit", observations=20,
        gross_spread_mean_pct=0.5, gross_spread_max_pct=1.0, net_spread_mean_pct=0.3, net_spread_max_pct=0.8,
        net_profit_per_1000usdt_mean=net_profit_mean, net_profit_per_1000usdt_max=net_profit_mean + 1,
        positive_rate_pct=70.0, mean_persistence_seconds=20.0, max_persistence_seconds=40.0,
        unique_detections=detections, continuations=continuations,
        best_observed_at=datetime(2026, 8, 24, tzinfo=UTC), status=OpportunityStatus.STRONG,
        net_profit_per_1000usdt_median=net_profit_mean, net_profit_per_1000usdt_p10=max(0.0, net_profit_mean - 1),
        net_profit_per_1000usdt_min=0.0, available_depth_usd_mean=500.0,
    )


class _FakeUniverse:
    def __init__(self, symbols):
        self.common_symbols = symbols


class _FakeUniverseBuilder:
    def __init__(self, symbols):
        self._symbols = symbols

    async def get_universe(self, force_refresh=False):
        return _FakeUniverse(self._symbols)


class _FakeStatus:
    def __init__(self, **kwargs):
        self.pairs_fast_scanned = kwargs.get("pairs_fast_scanned", 0)
        self.pairs_deep_validated = kwargs.get("pairs_deep_validated", 0)
        self.pairs_with_raw_edge = kwargs.get("pairs_with_raw_edge", 0)
        self.pairs_net_positive = kwargs.get("pairs_net_positive", 0)
        self.cycle_duration_seconds = kwargs.get("cycle_duration_seconds", 1.0)
        self.updated_at = kwargs.get("updated_at", datetime(2026, 8, 24, tzinfo=UTC).replace(tzinfo=None))


def _fake_async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


async def test_no_scan_status_yet_reports_zeros_not_crash(monkeypatch):
    monkeypatch.setattr(report_module, "live_universe_builder", _FakeUniverseBuilder(["ZRO/USDT"]))
    monkeypatch.setattr(report_module, "get_full_universe_scan_status", _fake_async(None))
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(AltcoinScanReport(window_start=None, window_end=None, total_observations=0)))

    report = await build_full_market_discovery_report(session=object())
    assert report.common_pairs == 1
    assert report.pairs_fast_scanned == 0
    assert report.scan_status_available is False


async def test_common_pairs_reflects_live_universe(monkeypatch):
    monkeypatch.setattr(report_module, "live_universe_builder", _FakeUniverseBuilder(["A/USDT", "B/USDT", "C/USDT"]))
    monkeypatch.setattr(report_module, "get_full_universe_scan_status", _fake_async(_FakeStatus(pairs_fast_scanned=3)))
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(AltcoinScanReport(window_start=None, window_end=None, total_observations=0)))

    report = await build_full_market_discovery_report(session=object())
    assert report.common_pairs == 3
    assert report.pairs_fast_scanned == 3
    assert report.scan_status_available is True


async def test_repeating_edge_requires_reuse_count_and_positive_mean(monkeypatch):
    monkeypatch.setattr(report_module, "live_universe_builder", _FakeUniverseBuilder([]))
    monkeypatch.setattr(report_module, "get_full_universe_scan_status", _fake_async(_FakeStatus()))
    scan_report = AltcoinScanReport(
        window_start=None, window_end=None, total_observations=40,
        best_direction_by_symbol=[
            _summary("A/USDT", net_profit_mean=3.0, detections=2, continuations=2),  # 4 sightings, positive -> repeating
            _summary("B/USDT", net_profit_mean=3.0, detections=0, continuations=0),  # 0 sightings -> not repeating
            _summary("C/USDT", net_profit_mean=-1.0, detections=5, continuations=5),  # negative edge -> not repeating
        ],
    )
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(scan_report))

    report = await build_full_market_discovery_report(session=object(), min_expected_reuse_count=3)
    assert report.pairs_with_repeating_net_edge == 1


async def test_top_10_sorted_by_mean_net_edge_descending(monkeypatch):
    monkeypatch.setattr(report_module, "live_universe_builder", _FakeUniverseBuilder([]))
    monkeypatch.setattr(report_module, "get_full_universe_scan_status", _fake_async(_FakeStatus()))
    scan_report = AltcoinScanReport(
        window_start=None, window_end=None, total_observations=40,
        best_direction_by_symbol=[
            _summary("LOW/USDT", net_profit_mean=1.0),
            _summary("HIGH/USDT", net_profit_mean=9.0),
            _summary("MID/USDT", net_profit_mean=5.0),
        ],
    )
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(scan_report))

    report = await build_full_market_discovery_report(session=object())
    assert [s.symbol for s in report.top_10_opportunities] == ["HIGH/USDT", "MID/USDT", "LOW/USDT"]


async def test_top_10_capped_at_ten(monkeypatch):
    monkeypatch.setattr(report_module, "live_universe_builder", _FakeUniverseBuilder([]))
    monkeypatch.setattr(report_module, "get_full_universe_scan_status", _fake_async(_FakeStatus()))
    scan_report = AltcoinScanReport(
        window_start=None, window_end=None, total_observations=200,
        best_direction_by_symbol=[_summary(f"S{i}/USDT", net_profit_mean=float(i)) for i in range(15)],
    )
    monkeypatch.setattr(report_module, "build_altcoin_scan_report", _fake_async(scan_report))

    report = await build_full_market_discovery_report(session=object())
    assert len(report.top_10_opportunities) == 10
