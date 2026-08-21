import time

import pytest

from app.config.constants import MarketType
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.market_data.store import MarketDataStore
from app.simulation.portfolios import VirtualPortfolio
from app.simulation.replay_engine import MarketEvent
from app.simulation.stress_testing import StressScenario, compute_robustness_score, run_stress_test


def _build_engines(store: MarketDataStore):
    return [CrossExchangeArbitrageEngine(assets=["BTC"], quote_asset="USDT", store=store, capital_usd=1_000)]


def _make_portfolio() -> VirtualPortfolio:
    return VirtualPortfolio(name="stress-test", initial_capital_usd=10_000, balances={"USDT": 10_000})


def _events() -> list[MarketEvent]:
    events = []
    for i in range(8):
        offset = i * 1.0
        events.append(
            MarketEvent("okx", MarketType.SPOT, "BTC/USDT", bid=49_990.0, ask=50_000.0, bid_quantity=5.0, ask_quantity=5.0, offset_seconds=offset)
        )
        events.append(
            MarketEvent("binance", MarketType.SPOT, "BTC/USDT", bid=50_400.0, ask=50_410.0, bid_quantity=5.0, ask_quantity=5.0, offset_seconds=offset)
        )
    return events


@pytest.mark.asyncio
async def test_stress_test_runs_all_three_scenarios():
    report = await run_stress_test(_events(), _build_engines, _make_portfolio, seed=42, replay_start=time.time())
    assert set(report.results.keys()) == {StressScenario.NORMAL, StressScenario.STRESS_1, StressScenario.STRESS_2}


@pytest.mark.asyncio
async def test_stress_test_is_deterministic_for_the_same_seed():
    start = time.time()
    report_a = await run_stress_test(_events(), _build_engines, _make_portfolio, seed=42, replay_start=start)
    report_b = await run_stress_test(_events(), _build_engines, _make_portfolio, seed=42, replay_start=start)
    assert report_a.net_profit_by_scenario_usd == report_b.net_profit_by_scenario_usd


def test_robustness_score_is_perfect_when_stress_pnl_matches_normal():
    assert compute_robustness_score(35.0, [35.0, 35.0]) == 100.0


def test_robustness_score_partial_when_stress_pnl_is_lower_but_still_positive():
    score = compute_robustness_score(35.0, [21.0, 8.0])
    assert 0 < score < 100


def test_robustness_score_is_penalized_hard_when_a_stress_scenario_turns_negative():
    survives = compute_robustness_score(35.0, [21.0, 8.0])
    fails = compute_robustness_score(35.0, [21.0, -8.0])
    assert fails < survives


def test_robustness_score_is_zero_when_normal_itself_is_unprofitable():
    assert compute_robustness_score(-5.0, [-10.0, -20.0]) == 0.0


def test_robustness_score_caps_at_100_even_if_stress_outperforms_normal():
    assert compute_robustness_score(10.0, [50.0, 50.0]) == 100.0
