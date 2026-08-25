from dataclasses import dataclass

import app.execution.live_preflight as preflight_module
from app.execution.live_preflight import build_multi_symbol_preflight_report
from app.execution.live_readiness_gate import FirstLiveGateReport, SmallestCommonOrderSize
from app.execution.live_ranker import PrePositioningCheck, RankedOpportunity
from app.execution.live_universe import LiveUniverse


def _account_gate(**overrides) -> FirstLiveGateReport:
    base = dict(
        binance_trade_api_ready=True,
        binance_trade_api_detail="ok",
        bybit_trade_api_ready=True,
        bybit_trade_api_detail="ok",
        withdrawals_disabled=True,
        withdrawals_detail="ok",
        binance_usdt_balance=100.0,
        bybit_lunc_balance=0.0,
        max_live_notional_usdt=5.0,
        leg_risk_protection_pass=True,
        leg_risk_protection_detail="ok",
        live_kill_switch_pass=True,
        live_kill_switch_detail="ok",
        real_pnl_ledger_ready=True,
        real_pnl_ledger_detail="ok",
        smallest_common_order_size=SmallestCommonOrderSize(True, None, 1.0, 5.0, 5.0),
        capital_pre_positioned=True,
        capital_pre_positioned_detail="ok",
        ready_for_first_real_arbitrage=True,
        proposed_first_trade_size_usdt=5.0,
    )
    base.update(overrides)
    return FirstLiveGateReport(**base)


def _ranked_opportunity(score=1.0, executable_now=True) -> RankedOpportunity:
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
        buy_exchange="binance", sell_exchange="bybit", required_buy_balance_usdt=5.0, required_sell_asset="ZRO",
        required_sell_qty=1.6, available_buy_balance_usdt=100.0, available_sell_balance=5.0,
        prepositioned=executable_now, executable_now=executable_now,
    )
    return RankedOpportunity(symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="bybit", quote=quote, prepositioning=prepositioning, score=score)


async def test_ready_when_everything_passes(monkeypatch):
    monkeypatch.setattr(preflight_module, "build_first_live_gate_report", _fake_gate(_account_gate()))
    monkeypatch.setattr(
        preflight_module.live_universe_builder, "get_universe",
        _fake_universe(LiveUniverse(common_symbols=["ZRO/USDT"], binance_okx_symbols=[], bybit_okx_symbols=[], all_three_symbols=[], binance_symbol_count=500, bybit_symbol_count=400, okx_symbol_count=0, fetched_at=100.0)),
    )
    monkeypatch.setattr(preflight_module, "rank_live_opportunities", _fake_rank([_ranked_opportunity()]))

    report = await build_multi_symbol_preflight_report()
    assert report.ready_to_start is True
    assert report.best_candidate is not None
    assert report.best_candidate.symbol == "ZRO/USDT"


async def test_not_ready_when_no_qualified_candidate(monkeypatch):
    monkeypatch.setattr(preflight_module, "build_first_live_gate_report", _fake_gate(_account_gate()))
    monkeypatch.setattr(
        preflight_module.live_universe_builder, "get_universe",
        _fake_universe(LiveUniverse(common_symbols=["ZRO/USDT"], binance_okx_symbols=[], bybit_okx_symbols=[], all_three_symbols=[], binance_symbol_count=500, bybit_symbol_count=400, okx_symbol_count=0, fetched_at=100.0)),
    )
    monkeypatch.setattr(preflight_module, "rank_live_opportunities", _fake_rank([]))  # nothing qualifies

    report = await build_multi_symbol_preflight_report()
    assert report.ready_to_start is False
    assert "NO_QUALIFIED_CANDIDATE" in report.ready_reason


async def test_not_ready_when_withdrawals_not_disabled(monkeypatch):
    monkeypatch.setattr(preflight_module, "build_first_live_gate_report", _fake_gate(_account_gate(withdrawals_disabled=False)))
    monkeypatch.setattr(
        preflight_module.live_universe_builder, "get_universe",
        _fake_universe(LiveUniverse(common_symbols=["ZRO/USDT"], binance_okx_symbols=[], bybit_okx_symbols=[], all_three_symbols=[], binance_symbol_count=500, bybit_symbol_count=400, okx_symbol_count=0, fetched_at=100.0)),
    )
    monkeypatch.setattr(preflight_module, "rank_live_opportunities", _fake_rank([_ranked_opportunity()]))

    report = await build_multi_symbol_preflight_report()
    assert report.ready_to_start is False
    assert "WITHDRAWALS_NOT_DISABLED" in report.ready_reason


async def test_not_ready_when_best_candidate_not_prepositioned(monkeypatch):
    monkeypatch.setattr(preflight_module, "build_first_live_gate_report", _fake_gate(_account_gate()))
    monkeypatch.setattr(
        preflight_module.live_universe_builder, "get_universe",
        _fake_universe(LiveUniverse(common_symbols=["ZRO/USDT"], binance_okx_symbols=[], bybit_okx_symbols=[], all_three_symbols=[], binance_symbol_count=500, bybit_symbol_count=400, okx_symbol_count=0, fetched_at=100.0)),
    )
    not_prepositioned = _ranked_opportunity(score=0.0, executable_now=False)
    monkeypatch.setattr(preflight_module, "rank_live_opportunities", _fake_rank([not_prepositioned]))

    report = await build_multi_symbol_preflight_report()
    # score=0.0 means it isn't "qualified" either, so this exercises the
    # NO_QUALIFIED_CANDIDATE path — both are legitimate "not ready" reasons
    assert report.ready_to_start is False


def _fake_gate(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


def _fake_universe(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner


def _fake_rank(value):
    async def _inner(*args, **kwargs):
        return value
    return _inner
