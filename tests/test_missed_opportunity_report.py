import uuid

import app.reporting.missed_opportunity_report as report_module
from app.execution.dual_leg_quote import DualLegQuote
from app.execution.live_ranker import PrePositioningCheck, RankedOpportunity
from app.reporting.missed_opportunity_report import _capital_busy_row, _inventory_missing_row, build_missed_opportunity_report
from app.scanner.missed_opportunity_tracker import CAUSE_FEES, CAUSE_INVENTORY_MISSING


def _quote(net_profit_usd=0.08, executable=True) -> DualLegQuote:
    return DualLegQuote(
        opportunity_id=uuid.uuid4(), symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="bybit",
        buy_execution_price=3.1, sell_execution_price=3.16, executable_qty=1.6, buy_valid_qty=1.6, sell_valid_qty=1.6,
        gross_spread_pct=1.9, buy_fee_usd=0.005, sell_fee_usd=0.005, buy_slippage_pct=0.0, sell_slippage_pct=0.0,
        buy_quote_age_ms=1.0, sell_quote_age_ms=1.0, dual_leg_latency_ms=100.0, net_profit_usd=net_profit_usd, net_return_bps=160.0,
        buy_min_notional_pass=True, buy_lot_size_pass=True, sell_min_notional_pass=True, sell_lot_size_pass=True,
        buy_tradable=True, sell_tradable=True, executable=executable, reason=None,
        buy_fee_source="real_account_fee", sell_fee_source="real_account_fee", computed_at=100.0,
    )


def _ranked(net_profit_usd=0.08, available_sell_balance=0.0, required_sell_qty=1.6, executable_now=False, symbol="ZRO/USDT") -> RankedOpportunity:
    quote = _quote(net_profit_usd=net_profit_usd)
    prepositioning = PrePositioningCheck(
        buy_exchange="binance", sell_exchange="bybit", required_buy_balance_usdt=5.0, required_sell_asset="ZRO",
        required_sell_qty=required_sell_qty, available_buy_balance_usdt=100.0, available_sell_balance=available_sell_balance,
        prepositioned=executable_now, executable_now=executable_now,
    )
    return RankedOpportunity(symbol=symbol, buy_exchange="binance", sell_exchange="bybit", quote=quote, prepositioning=prepositioning, score=1.0 if executable_now else 0.0)


# ---- _inventory_missing_row ------------------------------------------


def test_inventory_missing_counts_net_positive_blocked_only_by_inventory():
    ranked = [_ranked(net_profit_usd=0.08, available_sell_balance=0.0, required_sell_qty=1.6, executable_now=False)]
    row = _inventory_missing_row(ranked)
    assert row.count == 1
    assert row.theoretical_profit_usd_total == 0.08


def test_inventory_missing_excludes_non_positive_quotes():
    ranked = [_ranked(net_profit_usd=-0.02, available_sell_balance=0.0, required_sell_qty=1.6, executable_now=False)]
    row = _inventory_missing_row(ranked)
    assert row.count == 0


def test_inventory_missing_excludes_already_executable():
    ranked = [_ranked(net_profit_usd=0.08, available_sell_balance=2.0, required_sell_qty=1.6, executable_now=True)]
    row = _inventory_missing_row(ranked)
    assert row.count == 0


def test_inventory_missing_excludes_non_executable_quote():
    r = _ranked(net_profit_usd=0.08, available_sell_balance=0.0, required_sell_qty=1.6, executable_now=False)
    r.quote.executable = False
    row = _inventory_missing_row([r])
    assert row.count == 0


# ---- _capital_busy_row --------------------------------------------------


def test_capital_busy_zero_when_under_capacity():
    ranked = [_ranked(net_profit_usd=0.08, available_sell_balance=2.0, required_sell_qty=1.6, executable_now=True)]
    row = _capital_busy_row(ranked, in_flight_count=0, max_concurrent=1)
    assert row.count == 0
    assert row.theoretical_profit_usd_total == 0.0


def test_capital_busy_counts_qualified_when_at_capacity():
    ranked = [
        _ranked(net_profit_usd=0.08, available_sell_balance=2.0, required_sell_qty=1.6, executable_now=True, symbol="A/USDT"),
        _ranked(net_profit_usd=0.05, available_sell_balance=2.0, required_sell_qty=1.6, executable_now=True, symbol="B/USDT"),
    ]
    row = _capital_busy_row(ranked, in_flight_count=1, max_concurrent=1)
    assert row.count == 2
    assert row.theoretical_profit_usd_total == 0.13


def test_capital_busy_excludes_not_prepositioned_even_at_capacity():
    ranked = [_ranked(net_profit_usd=0.08, available_sell_balance=0.0, required_sell_qty=1.6, executable_now=False)]
    row = _capital_busy_row(ranked, in_flight_count=1, max_concurrent=1)
    assert row.count == 0  # this one belongs to INVENTORY_MISSING, not CAPITAL_BUSY


# ---- build_missed_opportunity_report end-to-end (monkeypatched I/O) ----


def _fake_async(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


class _FakeSummaryRow:
    def __init__(self, cause, count, theoretical_profit_usd_total):
        self.cause = cause
        self.count = count
        self.theoretical_profit_usd_total = theoretical_profit_usd_total


async def test_build_report_merges_persisted_and_live_causes(monkeypatch):
    persisted = [_FakeSummaryRow(CAUSE_FEES, 12, 0.0)]
    monkeypatch.setattr(report_module, "get_missed_opportunity_summaries", _fake_async(persisted))
    ranked = [_ranked(net_profit_usd=0.08, available_sell_balance=0.0, required_sell_qty=1.6, executable_now=False)]
    monkeypatch.setattr(report_module, "rank_live_opportunities", _fake_async(ranked))

    report = await build_missed_opportunity_report(session=object())
    by_cause = {r.cause: r for r in report.causes}
    assert by_cause[CAUSE_FEES].count == 12
    assert by_cause[CAUSE_INVENTORY_MISSING].count == 1
    assert report.total_missed == 13
    assert report.primary_cause == CAUSE_FEES  # highest count


async def test_build_report_all_causes_present_even_when_zero(monkeypatch):
    monkeypatch.setattr(report_module, "get_missed_opportunity_summaries", _fake_async([]))
    monkeypatch.setattr(report_module, "rank_live_opportunities", _fake_async([]))

    report = await build_missed_opportunity_report(session=object())
    from app.scanner.missed_opportunity_tracker import ALL_CAUSES

    causes_present = {r.cause for r in report.causes}
    assert causes_present == set(ALL_CAUSES)
    assert report.total_missed == 0
    assert report.primary_cause is None


async def test_build_report_survives_ranker_failure(monkeypatch):
    monkeypatch.setattr(report_module, "get_missed_opportunity_summaries", _fake_async([_FakeSummaryRow(CAUSE_FEES, 5, 0.0)]))

    async def _raise(*args, **kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(report_module, "rank_live_opportunities", _raise)

    report = await build_missed_opportunity_report(session=object())
    by_cause = {r.cause: r for r in report.causes}
    assert by_cause[CAUSE_FEES].count == 5
    assert by_cause[CAUSE_INVENTORY_MISSING].count == 0  # never raises even though the ranker did
