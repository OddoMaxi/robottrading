from datetime import UTC, datetime

import app.execution.inventory_manager as inv_module
from app.execution.inventory_manager import (
    InventoryClassification,
    OpportunityInventoryCheck,
    _to_opportunity_check,
    build_inventory_report,
    is_preposition_eligible,
    recommend_rebalance,
    score_direction_for_inventory,
)
from app.execution.live_ranker import PrePositioningCheck, RankedOpportunity
from app.reporting.altcoin_scan_report import DirectionSummary, OpportunityStatus
from app.reporting.short_term_regime import ShortTermRegime, ShortTermRegimeSummary


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
        net_profit_per_1000usdt_median=3.0,
        net_profit_per_1000usdt_p10=1.5,  # positive — a genuinely consistent case by default
        net_profit_per_1000usdt_min=0.5,
        available_depth_usd_mean=500.0,
    )
    base.update(overrides)
    return DirectionSummary(**base)


def _short_term(regime=ShortTermRegime.CONFIRMED_SHORT_TERM, **overrides) -> ShortTermRegimeSummary:
    base = dict(
        symbol="ZRO/USDT",
        buy_exchange="binance",
        sell_exchange="bybit",
        edge_now_positive=True,
        edge_now_net_profit_per_1000usdt=3.0,
        current_streak_seconds=20.0,
        windows={},
        confirmations_recent=3,
        regime=regime,
        regime_reason="test fixture",
    )
    base.update(overrides)
    return ShortTermRegimeSummary(**base)


# ---- score_direction_for_inventory (FINAL SIMPLIFICATION — regime-driven) --
#
# Classification is now driven entirely by short_term.regime, not by the
# 24h summary (item 2/6 of the 2026-08-24 "FINAL SIMPLIFICATION"
# directive: the 24h/1h window is analytics-only and must never veto a
# currently-confirmed short-term opportunity — this is exactly what
# wrongly blocked the real RVN opportunity this directive fixes).


def test_no_short_term_data_defaults_to_do_not_preposition():
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3)
    assert is_preposition_eligible(score) is False
    assert score.classification == InventoryClassification.DO_NOT_PREPOSITION
    assert score.short_term_regime == "NO_DATA"


def test_no_edge_regime_is_do_not_preposition():
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.NO_EDGE, edge_now_positive=False))
    assert is_preposition_eligible(score) is False
    assert score.classification == InventoryClassification.DO_NOT_PREPOSITION


def test_flash_regime_is_observe_not_candidate():
    """Directive's own words: never buy just to chase a spread that's
    already disappearing — positive right now but not yet independently
    confirmed enough times must not qualify for real capital yet."""
    score = score_direction_for_inventory(
        _summary(), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.FLASH, confirmations_recent=1, regime_reason="edge positive now but only 1 confirmation(s)")
    )
    assert is_preposition_eligible(score) is False
    assert score.classification == InventoryClassification.OBSERVE
    assert "confirmation" in score.reason


def test_confirmed_short_term_regime_is_preposition_candidate():
    """Item 4: CONFIRMED_SHORT_TERM must already be tradable — it is not
    made to wait for multi-minute persistence."""
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.CONFIRMED_SHORT_TERM))
    assert is_preposition_eligible(score) is True
    assert score.classification == InventoryClassification.PREPOSITION_CANDIDATE


def test_persistent_regime_is_strong_preposition_candidate():
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.PERSISTENT))
    assert is_preposition_eligible(score) is True
    assert score.classification == InventoryClassification.STRONG_PREPOSITION_CANDIDATE


def test_strong_persistent_regime_is_strong_preposition_candidate():
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.STRONG_PERSISTENT))
    assert is_preposition_eligible(score) is True
    assert score.classification == InventoryClassification.STRONG_PREPOSITION_CANDIDATE


def test_negative_24h_mean_does_not_block_confirmed_short_term():
    """The exact RVN regression this directive fixes: a negative 24h mean
    must NEVER by itself veto a symbol that is CONFIRMED_SHORT_TERM right
    now."""
    score = score_direction_for_inventory(
        _summary(net_profit_per_1000usdt_mean=-5.0), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.CONFIRMED_SHORT_TERM)
    )
    assert is_preposition_eligible(score) is True
    assert score.classification == InventoryClassification.PREPOSITION_CANDIDATE
    assert score.mean_net_profit_24h_usdt == -5.0  # still recorded as analytics, just not a gate


def test_negative_24h_p10_does_not_block_confirmed_short_term():
    score = score_direction_for_inventory(
        _summary(net_profit_per_1000usdt_p10=-9.0), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.CONFIRMED_SHORT_TERM)
    )
    assert is_preposition_eligible(score) is True
    assert score.classification == InventoryClassification.PREPOSITION_CANDIDATE


def test_weak_24h_status_does_not_block_confirmed_short_term():
    """status (OpportunityStatus, itself derived from 24h data) is no
    longer part of the classification decision at all."""
    score = score_direction_for_inventory(
        _summary(status=OpportunityStatus.WEAK), min_expected_reuse_count=3, short_term=_short_term(regime=ShortTermRegime.CONFIRMED_SHORT_TERM)
    )
    assert is_preposition_eligible(score) is True
    assert score.classification == InventoryClassification.PREPOSITION_CANDIDATE


def test_score_breakdown_carries_the_short_term_fields():
    st = _short_term(regime=ShortTermRegime.PERSISTENT, edge_now_net_profit_per_1000usdt=7.5, confirmations_recent=5, current_streak_seconds=310.0, mean_net_profit_1h_usdt=2.0)
    score = score_direction_for_inventory(_summary(), min_expected_reuse_count=3, short_term=st)
    assert score.short_term_regime == "PERSISTENT"
    assert score.edge_now_net_profit_per_1000usdt == 7.5
    assert score.confirmations_recent == 5
    assert score.current_streak_seconds == 310.0
    assert score.mean_net_profit_1h_usdt == 2.0


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
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3, short_term=_short_term())
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
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3, short_term=_short_term())
    recs = recommend_rebalance(
        scores=[score], missing=[], current_inventory_usdt_value={},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    assert [r for r in recs if r.action == "BUY_INVENTORY"] == []


def test_no_buy_recommended_for_ineligible_symbol_even_if_missing():
    score = score_direction_for_inventory(
        _summary(symbol="ZRO/USDT"), min_expected_reuse_count=3,
        short_term=_short_term(regime=ShortTermRegime.FLASH, confirmations_recent=1),
    )
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={},
        binance_usdt=50.0, bybit_usdt=50.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=40.0, max_rebalance_size_usdt=5.0,
    )
    assert [r for r in recs if r.action == "BUY_INVENTORY"] == []


def test_buy_size_never_exceeds_max_rebalance_size():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3, short_term=_short_term())
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={},
        binance_usdt=100.0, bybit_usdt=100.0,
        max_inventory_per_asset_usdt=50.0, max_total_inventory_exposure_usdt=50.0, max_rebalance_size_usdt=3.0,
    )
    buy = next(r for r in recs if r.action == "BUY_INVENTORY")
    assert buy.recommended_notional_usdt <= 3.0


def test_buy_size_never_exceeds_max_inventory_per_asset_headroom():
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3, short_term=_short_term())
    recs = recommend_rebalance(
        scores=[score], missing=[_missing_check("ZRO")], current_inventory_usdt_value={"ZRO": 8.0},
        binance_usdt=100.0, bybit_usdt=100.0,
        max_inventory_per_asset_usdt=10.0, max_total_inventory_exposure_usdt=50.0, max_rebalance_size_usdt=5.0,
    )
    buy = next(r for r in recs if r.action == "BUY_INVENTORY")
    assert buy.recommended_notional_usdt <= 2.0  # only 10 - 8 = 2 headroom left


def test_total_exposure_cap_stops_further_buys():
    scores = [
        score_direction_for_inventory(_summary(symbol="ZRO/USDT", net_profit_per_1000usdt_mean=5.0), min_expected_reuse_count=3, short_term=_short_term(symbol="ZRO/USDT")),
        score_direction_for_inventory(_summary(symbol="STX/USDT", net_profit_per_1000usdt_mean=4.0), min_expected_reuse_count=3, short_term=_short_term(symbol="STX/USDT")),
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
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3, short_term=_short_term())
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
    score = score_direction_for_inventory(_summary(symbol="ZRO/USDT"), min_expected_reuse_count=3, short_term=_short_term())
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
    monkeypatch.setattr(inv_module, "build_short_term_regimes", _fake_async({("ZRO/USDT", "binance", "bybit"): _short_term(symbol="ZRO/USDT")}))

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
    monkeypatch.setattr(inv_module, "build_short_term_regimes", _fake_async({}))

    report = await build_inventory_report(session=object(), binance_read=FakeBinanceRead(usdt=100.0), bybit_read=FakeBybitRead())
    assert report.capital_locked_in_inventory_usdt == 0.0
    assert report.rebalance_candidates == []


async def test_since_passed_to_scan_report_is_tz_naive(monkeypatch):
    """Regression: AltcoinScanObservationRecord.observed_at is stored
    tz-naive (app.api.routes.scanner_altcoin_report's own convention) —
    asyncpg raises DataError ("can't subtract offset-naive and
    offset-aware datetimes") if since= is passed tz-aware. Caught live
    against the real VPS database (2026-08-24) after the unit tests,
    which all monkeypatch build_altcoin_scan_report and so never
    exercised the real asyncpg comparison, missed it."""
    monkeypatch.setattr(inv_module, "rank_live_opportunities", _fake_async([]))

    captured = {}

    async def _capture(session, since=None, until=None):
        captured["since"] = since
        from app.reporting.altcoin_scan_report import AltcoinScanReport

        return AltcoinScanReport(window_start=None, window_end=None, total_observations=0, best_direction_by_symbol=[])

    monkeypatch.setattr(inv_module, "build_altcoin_scan_report", _capture)
    monkeypatch.setattr(inv_module, "build_short_term_regimes", _fake_async({}))

    await build_inventory_report(session=object(), binance_read=FakeBinanceRead(usdt=100.0), bybit_read=FakeBybitRead())
    assert captured["since"] is not None
    assert captured["since"].tzinfo is None
