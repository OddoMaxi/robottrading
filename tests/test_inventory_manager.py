from datetime import UTC, datetime

import app.execution.inventory_manager as inv_module
from app.execution.inventory_manager import (
    OpportunityInventoryCheck,
    _to_opportunity_check,
    build_inventory_report,
    recommend_rebalance,
    score_direction_for_inventory,
)
from app.execution.live_ranker import PrePositioningCheck, RankedOpportunity
from app.reporting.altcoin_scan_report import DirectionSummary, OpportunityStatus


def _summary(**overrides) -> DirectionSummary:
    base = dict(
        symbol="ZRO/USDT",
        buy_exchange="binance",
        sell_exchange="bybit",
        observations=20,
        gross_spread_mean_pct=0.5,
        gross_spread_max_pct=1.2,
        net_spread_mean_pct=0.3,
        net_spread_max_pct=0.9,
        net_profit_per_1000usdt_mean=3.0,
        net_profit_per_1000usdt_max=5.0,
        positive_rate_pct=80.0,
        mean_persistence_seconds=30.0,
        max_persistence_seconds=60.0,
        unique_detections=8,
        continuations=6,
        best_observed_at=datetime(2026, 8, 23, tzinfo=UTC),
        status=OpportunityStatus.STRONG,
    )
    base.update(overrides)
    return DirectionSummary(**base)


# ---- score_direction_for_inventory ----------------------------------


def test_strong_recurring_symbol_is_eligible():
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3)
    assert score.eligible_for_prepositioning is True
    assert 0.0 < score.total_score <= 100.0
    assert score.base_asset == "ZRO"


def test_insufficient_observations_not_eligible():
    score = score_direction_for_inventory(_summary(observations=2), min_expected_reuse_count=3)
    assert score.eligible_for_prepositioning is False
    assert "insufficient history" in score.reason


def test_one_off_opportunity_never_earns_prepositioning():
    """Directive's own words: never buy just to chase a spread that's
    already disappearing — a symbol seen only once or twice must not
    qualify even if everything else about it looks great."""
    score = score_direction_for_inventory(_summary(unique_detections=1, continuations=0), min_expected_reuse_count=3)
    assert score.eligible_for_prepositioning is False
    assert "one-off" in score.reason


def test_non_positive_edge_not_eligible():
    score = score_direction_for_inventory(_summary(net_profit_per_1000usdt_mean=0.0), min_expected_reuse_count=3)
    assert score.eligible_for_prepositioning is False
    assert "net-positive" in score.reason


def test_weak_status_not_eligible_even_with_enough_sightings():
    score = score_direction_for_inventory(
        _summary(status=OpportunityStatus.WEAK, unique_detections=10, continuations=10, net_profit_per_1000usdt_mean=3.0),
        min_expected_reuse_count=3,
    )
    assert score.eligible_for_prepositioning is False
    assert "status too weak" in score.reason


def test_higher_edge_scores_higher_all_else_equal():
    weak = score_direction_for_inventory(_summary(net_profit_per_1000usdt_mean=0.5), min_expected_reuse_count=3)
    strong = score_direction_for_inventory(_summary(net_profit_per_1000usdt_mean=4.0), min_expected_reuse_count=3)
    assert strong.total_score > weak.total_score


# ---- _to_opportunity_check --------------------------------------------


def _ranked(available_sell_balance, available_buy_balance_usdt, required_sell_qty=1.6, required_buy_balance_usdt=5.0):
    from app.execution.dual_leg_quote import DualLegQuote
    import uuid

    quote = DualLegQuote(
        opportunity_id=uuid.uuid4(), symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="bybit",
        buy_execution_price=3.1, sell_execution_price=3.16, executable_qty=1.6, buy_valid_qty=1.6, sell_valid_qty=1.6,
        gross_spread_pct=1.9, buy_fee_usd=0.005, sell_fee_usd=0.005, buy_slippage_pct=0.0, sell_slippage_pct=0.0,
        buy_quote_age_ms=1.0, sell_quote_age_ms=1.0, dual_leg_latency_ms=100.0, net_profit_usd=0.08, net_return_bps=160.0,
        buy_min_notional_pass=True, buy_lot_size_pass=True, sell_min_notional_pass=True, sell_lot_size_pass=True,
        buy_tradable=True, sell_tradable=True, executable=True, reason=None,
        buy_fee_source="real_account_fee", sell_fee_source="real_account_fee", computed_at=100.0,
    )
    prepositioning = PrePositioningCheck(
        buy_exchange="binance", sell_exchange="bybit",
        required_buy_balance_usdt=required_buy_balance_usdt, required_sell_asset="ZRO", required_sell_qty=required_sell_qty,
        available_buy_balance_usdt=available_buy_balance_usdt, available_sell_balance=available_sell_balance,
        prepositioned=available_sell_balance >= required_sell_qty and available_buy_balance_usdt >= required_buy_balance_usdt,
        executable_now=available_sell_balance >= required_sell_qty and available_buy_balance_usdt >= required_buy_balance_usdt,
    )
    return RankedOpportunity(symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="bybit", quote=quote, prepositioning=prepositioning, score=1.0)


def test_opportunity_check_executable_when_both_sides_funded():
    check = _to_opportunity_check(_ranked(available_sell_balance=2.0, available_buy_balance_usdt=10.0))
    assert check.status == "EXECUTABLE_NOW"
    assert check.reason is None
    assert check.inventory_ready is True


def test_opportunity_check_inventory_missing_when_base_asset_absent():
    check = _to_opportunity_check(_ranked(available_sell_balance=0.0, available_buy_balance_usdt=10.0))
    assert check.status == "NOT_EXECUTABLE_NOW"
    assert check.reason == "INVENTORY_MISSING"
    assert check.inventory_ready is False


def test_opportunity_check_insufficient_buy_capital_when_inventory_present_but_no_usdt():
    check = _to_opportunity_check(_ranked(available_sell_balance=2.0, available_buy_balance_usdt=0.0))
    assert check.status == "NOT_EXECUTABLE_NOW"
    assert check.reason == "INSUFFICIENT_BUY_CAPITAL"
    assert check.inventory_ready is True


# ---- recommend_rebalance ----------------------------------------------


def _missing_check(asset="ZRO", sell_exchange="bybit") -> OpportunityInventoryCheck:
    return OpportunityInventoryCheck(
        symbol=f"{asset}/USDT", buy_exchange="binance", sell_exchange=sell_exchange,
        required_quote_asset="USDT", required_quote_amount=5.0, required_base_asset=asset, required_base_amount=1.6,
        current_base_inventory=0.0, inventory_ready=False, status="NOT_EXECUTABLE_NOW", reason="INVENTORY_MISSING",
    )


def test_buys_inventory_for_top_eligible_missing_candidate():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    buys = [r for r in recs if r.action == "BUY_INVENTORY"]
    assert len(buys) == 1
    assert buys[0].asset == "ZRO"
    assert buys[0].exchange == "bybit"  # the missing check's sell_exchange
    assert buys[0].simulated is True


def test_no_buy_recommended_when_not_currently_missing():
    """An asset with a great score but nothing currently blocking it
    (not in `missing`) gets no recommendation — MASTER never buys
    speculative inventory just because a symbol scores well."""
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[], current_inventory_usdt_value={},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    assert [r for r in recs if r.action == "BUY_INVENTORY"] == []


def test_no_buy_recommended_for_ineligible_symbol_even_if_missing():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT", unique_detections=1, continuations=0), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    assert [r for r in recs if r.action == "BUY_INVENTORY"] == []


def test_buy_size_never_exceeds_max_rebalance_size():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={},
        binance_usdt=100.0, bybit_usdt=100.0,
        max_inventory_per_asset_usdt=50.0, max_total_inventory_exposure_usdt=50.0, max_rebalance_size_usdt=3.0,
    )
    buy = next(r for r in recs if r.action == "BUY_INVENTORY")
    assert buy.recommended_notional_usdt <= 3.0


def test_buy_size_never_exceeds_max_inventory_per_asset_headroom():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={"ZRO": 8.0},
        binance_usdt=100.0, bybit_usdt=100.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=50.0, max_rebalance_size_usdt=5.0,
    )
    buy = next(r for r in recs if r.action == "BUY_INVENTORY")
    assert buy.recommended_notional_usdt <= 2.0  # only 10 - 8 = 2 headroom left


def test_total_exposure_cap_stops_further_buys():
    scores = [
        score_direction_for_inventory(_summary(symbol="ZRO/USDT", net_profit_per_1000usdt_mean=5.0), min_expected_reuse_count=3),
        score_direction_for_inventory(_summary(symbol="STX/USDT", net_profit_per_1000usdt_mean=4.0), min_expected_reuse_count=3),
    ]
    recs = recommend_rebalance(
        scores=scores, missing=[_missing_check("ZRO"), _missing_check("STX")], current_inventory_usdt_value={},
        binance_usdt=100.0, bybit_usdt=100.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=5.0, max_rebalance_size_usdt=5.0,
    )
    buys = [r for r in recs if r.action == "BUY_INVENTORY"]
    assert len(buys) == 1  # exposure cap exhausted after the first (higher-scoring) buy
    assert buys[0].asset == "ZRO"
    assert sum(r.recommended_notional_usdt for r in buys) <= 5.0


def test_never_recommends_buying_more_than_available_usdt_on_that_exchange():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO", sell_exchange="bybit")], current_inventory_usdt_value={},
        binance_usdt=100.0, bybit_usdt=1.5,  # starved sell-exchange
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    buy = next(r for r in recs if r.action == "BUY_INVENTORY")
    assert buy.recommended_notional_usdt <= 1.5


def test_sells_stale_inventory_no_longer_eligible():
    score = score_direction_for_inventory(_summary(symbol="OLD/USDT", unique_detections=0, continuations=0), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[], current_inventory_usdt_value={"OLD": 6.0},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    sells = [r for r in recs if r.action == "SELL_INVENTORY"]
    assert len(sells) == 1
    assert sells[0].asset == "OLD"
    assert sells[0].simulated is True


def test_no_sell_recommended_for_still_eligible_holding():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3)
    recs = recommend_rebalance(
        scores=[score], missing=[], current_inventory_usdt_value={"ZRO": 6.0},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    assert [r for r in recs if r.action == "SELL_INVENTORY"] == []


def test_zero_or_negative_holdings_produce_no_sell_recommendation():
    recs = recommend_rebalance(
        scores=[], missing=[], current_inventory_usdt_value={"DUST": 0.0},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    assert recs == []


# ---- build_inventory_report end-to-end (monkeypatched I/O) -----------


class _FakeSnapshot:
    def __init__(self, usdt=0.0, balances=None):
        self._usdt = usdt
        self.balances = balances or []

    def balance_usdt(self):
        return self._usdt


class _FakeBalance:
    def __init__(self, asset, free):
        self.asset = asset
        self.free = free


class FakeBinanceRead:
    def __init__(self, usdt=100.0, balances=None):
        self._snapshot = _FakeSnapshot(usdt, balances)

    async def get_account_snapshot(self):
        return self._snapshot


class FakeBybitRead:
    def __init__(self, wallet=None):
        self._wallet = wallet or {"result": {"list": [{"coin": [{"coin": "USDT", "availableToWithdraw": "60"}]}]}}

    async def get_wallet_balance(self):
        return self._wallet


def _fake_async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


async def test_build_inventory_report_end_to_end(monkeypatch):
    ranked = [_ranked(available_sell_balance=0.0, available_buy_balance_usdt=100.0)]  # inventory missing on ZRO
    monkeypatch.setattr(inv_module, "rank_live_opportunities", _fake_async(ranked))

    from app.reporting.altcoin_scan_report import AltcoinScanReport

    report_stub = AltcoinScanReport(window_start=None, window_end=None, total_observations=20, best_direction_by_symbol=[_summary(symbol="ZRO/USDT")])
    monkeypatch.setattr(inv_module, "build_altcoin_scan_report", _fake_async(report_stub))

    report = await build_inventory_report(
        session=object(),
        binance_read=FakeBinanceRead(usdt=100.0),
        bybit_read=FakeBybitRead(),
    )

    assert report.simulation_only is True
    assert report.total_usdt_available == 160.0
    assert report.inventory_pnl_usd is None
    assert any(c.reason == "INVENTORY_MISSING" for c in report.inventory_missing)
    buys = [r for r in report.rebalance_candidates if r.action == "BUY_INVENTORY"]
    assert len(buys) == 1
    assert buys[0].asset == "ZRO"


async def test_build_inventory_report_no_holdings_means_zero_locked_capital(monkeypatch):
    monkeypatch.setattr(inv_module, "rank_live_opportunities", _fake_async([]))

    from app.reporting.altcoin_scan_report import AltcoinScanReport

    monkeypatch.setattr(
        inv_module, "build_altcoin_scan_report",
        _fake_async(AltcoinScanReport(window_start=None, window_end=None, total_observations=0, best_direction_by_symbol=[])),
    )

    report = await build_inventory_report(session=object(), binance_read=FakeBinanceRead(usdt=100.0), bybit_read=FakeBybitRead())
    assert report.capital_locked_in_inventory_usdt == 0.0
    assert report.rebalance_candidates == []
