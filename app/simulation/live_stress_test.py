"""Live Stress Test (Reality Engine spec, sections 46-47) — runs the Stress
Testing engine (app.simulation.stress_testing) against a snapshot of
*currently observed* market quotes, so "how robust is this strategy right
now" doesn't require a recorded historical dataset (Data Collection for
Future AI, section 51, isn't built yet).

Scoped to the three quote-driven engines (Stablecoin, Cross-Exchange,
Triangular) — Funding/Basis hold positions for days, where a few hundred ms
of latency stress is economically irrelevant (see the staleness/break-even
margins already used in app/engines/funding.py and basis.py), so they're
left out rather than forced through a stress dimension that means nothing
for them.

Fully read-only with respect to the live engine: snapshots the live
MarketDataStore's quotes, then replays them through a private store/
portfolios/trader — never touches the live portfolios, position tracker,
or risk engine.
"""

import time

from app.config.constants import PRIORITY_EXCHANGES
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.engines.stablecoin import StablecoinArbitrageEngine
from app.engines.triangular import TriangularArbitrageEngine
from app.market_data.store import MarketDataStore
from app.market_data.store import market_data_store as live_market_data_store
from app.simulation.portfolios import VirtualPortfolio
from app.simulation.replay_engine import MarketEvent
from app.simulation.stress_testing import StressTestReport, run_stress_test

LIVE_STRESS_TEST_TRADE_CAPITAL_USD = 1_000
LIVE_STRESS_TEST_PORTFOLIO_USD = 10_000


def snapshot_live_quotes_as_events(store: MarketDataStore) -> list[MarketEvent]:
    return [
        MarketEvent(
            exchange=quote.exchange,
            market=quote.market,
            symbol=quote.symbol,
            bid=quote.bid,
            ask=quote.ask,
            bid_quantity=quote.bid_quantity,
            ask_quantity=quote.ask_quantity,
            offset_seconds=0.0,
        )
        for quote in store.all_quotes()
    ]


def _build_engines(store: MarketDataStore):
    return [
        StablecoinArbitrageEngine(store=store, capital_usd=LIVE_STRESS_TEST_TRADE_CAPITAL_USD),
        CrossExchangeArbitrageEngine(store=store, capital_usd=LIVE_STRESS_TEST_TRADE_CAPITAL_USD),
        *(
            TriangularArbitrageEngine(exchange=exchange, store=store, capital_usd=LIVE_STRESS_TEST_TRADE_CAPITAL_USD)
            for exchange in PRIORITY_EXCHANGES
        ),
    ]


def _make_portfolio() -> VirtualPortfolio:
    return VirtualPortfolio(
        name="stress-test", initial_capital_usd=LIVE_STRESS_TEST_PORTFOLIO_USD, balances={"USDT": LIVE_STRESS_TEST_PORTFOLIO_USD}
    )


async def run_live_stress_test(seed: int = 0, now: float | None = None) -> StressTestReport:
    events = snapshot_live_quotes_as_events(live_market_data_store)
    return await run_stress_test(events, _build_engines, _make_portfolio, seed=seed, replay_start=now if now is not None else time.time())
