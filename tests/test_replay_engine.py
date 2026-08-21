import time

import pytest

from app.config.constants import MarketType
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.execution.latency_engine import LatencyProfile
from app.market_data.store import MarketDataStore
from app.simulation.portfolios import VirtualPortfolio
from app.simulation.replay_engine import MarketEvent, run_replay


def _build_engines(store: MarketDataStore):
    return [CrossExchangeArbitrageEngine(assets=["BTC"], quote_asset="USDT", store=store, capital_usd=1_000)]


def _events() -> list[MarketEvent]:
    """A profitable, repeating cross-exchange spread on BTC/USDT: cheap on
    exchange A, expensive on exchange B, replayed across several ticks so
    a position can open, expire, and re-open within the run."""
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
async def test_replay_detects_and_trades_a_profitable_spread():
    portfolio = VirtualPortfolio(name="replay-test", initial_capital_usd=10_000, balances={"USDT": 10_000})
    start = time.time()
    result = await run_replay(_events(), _build_engines, portfolio, seed=42, replay_start=start)

    assert result.opportunities_detected > 0
    assert result.trades_executed > 0


@pytest.mark.asyncio
async def test_same_seed_and_events_produce_an_identical_result():
    """Section 44's own requirement: same dataset + same configuration = same result."""
    start = time.time()

    portfolio_a = VirtualPortfolio(name="replay-a", initial_capital_usd=10_000, balances={"USDT": 10_000})
    result_a = await run_replay(_events(), _build_engines, portfolio_a, seed=7, replay_start=start)

    portfolio_b = VirtualPortfolio(name="replay-b", initial_capital_usd=10_000, balances={"USDT": 10_000})
    result_b = await run_replay(_events(), _build_engines, portfolio_b, seed=7, replay_start=start)

    assert result_a.as_status_tuple() == result_b.as_status_tuple()
    assert result_a.net_profit_usd == result_b.net_profit_usd
    assert result_a.opportunities_detected == result_b.opportunities_detected


@pytest.mark.asyncio
async def test_different_seeds_can_diverge_in_execution_outcome():
    """Not a hard guarantee for every possible dataset, but for this
    fixture (several ticks, real slippage/leg-failure randomness) two very
    different seeds should not coincidentally land on the exact same
    per-trade profit figures every time."""
    start = time.time()

    portfolio_a = VirtualPortfolio(name="replay-a", initial_capital_usd=10_000, balances={"USDT": 10_000})
    result_a = await run_replay(_events(), _build_engines, portfolio_a, seed=1, replay_start=start)

    portfolio_b = VirtualPortfolio(name="replay-b", initial_capital_usd=10_000, balances={"USDT": 10_000})
    result_b = await run_replay(_events(), _build_engines, portfolio_b, seed=99999, replay_start=start)

    assert result_a.as_status_tuple() != result_b.as_status_tuple()


@pytest.mark.asyncio
async def test_a_stress_latency_profile_replays_deterministically_too():
    """Stress Testing substrate (section 46) — replaying under a non-default
    LatencyProfile is just another configuration; it must be exactly as
    deterministic as the default profile (same events+seed+profile = same
    result), which is what Stress Testing's P&L comparison relies on."""
    start = time.time()

    portfolio_a = VirtualPortfolio(name="stress-a", initial_capital_usd=10_000, balances={"USDT": 10_000})
    result_a = await run_replay(_events(), _build_engines, portfolio_a, seed=13, replay_start=start, latency_profile=LatencyProfile.STRESS)

    portfolio_b = VirtualPortfolio(name="stress-b", initial_capital_usd=10_000, balances={"USDT": 10_000})
    result_b = await run_replay(_events(), _build_engines, portfolio_b, seed=13, replay_start=start, latency_profile=LatencyProfile.STRESS)

    assert result_a.as_status_tuple() == result_b.as_status_tuple()
