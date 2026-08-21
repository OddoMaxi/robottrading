import time

import pytest

from app.config.constants import MarketType
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore
from app.simulation.live_stress_test import run_live_stress_test, snapshot_live_quotes_as_events
from app.simulation.stress_testing import StressScenario


def test_snapshot_live_quotes_as_events_captures_every_present_quote():
    store = MarketDataStore()
    now = time.time()
    store.update_quote(NormalizedQuote("okx", MarketType.SPOT, "BTC/USDT", 49_990.0, 50_000.0, 5.0, 5.0, now, now))
    store.update_quote(NormalizedQuote("binance", MarketType.SPOT, "BTC/USDT", 50_400.0, 50_410.0, 5.0, 5.0, now, now))

    events = snapshot_live_quotes_as_events(store)

    assert len(events) == 2
    assert {e.exchange for e in events} == {"okx", "binance"}
    assert all(e.offset_seconds == 0.0 for e in events)


@pytest.mark.asyncio
async def test_run_live_stress_test_against_an_empty_market_reports_no_profit_and_zero_robustness():
    """With no live quotes at all (as in a test process that never started
    a collector), nothing gets detected in any scenario — the report must
    say so cleanly rather than raising."""
    report = await run_live_stress_test(seed=1)

    assert report.net_profit_by_scenario_usd[StressScenario.NORMAL] == 0.0
    assert report.robustness_score == 0.0
