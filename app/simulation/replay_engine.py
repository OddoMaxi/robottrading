"""Market Replay Engine (Reality Engine spec, sections 43-45).

Feeds a fixed, ordered sequence of market quotes through the exact same
detect -> validate -> paper-trade pipeline the live engine uses (main.py's
detection_loop), just driven by a recorded/synthetic MarketEvent list
instead of live WebSocket collectors, against a private MarketDataStore so
a replay never touches the live one.

Determinism (section 44 — "same dataset + same configuration = same
result") comes from seeding PaperTrader's RNG explicitly: replaying the
same events with the same seed and the same engines/latency profile always
produces the same trades, proven by tests/test_replay_engine.py running a
replay twice and diffing the output.

This is also the substrate Stress Testing (section 46) runs on: replay the
same events under different LatencyProfile values and compare the
resulting P&L.
"""

import random
from dataclasses import dataclass, field

from app.config.constants import MarketType
from app.engines.base import ArbitrageEngine
from app.execution.latency_engine import DEFAULT_PROFILE, LatencyProfile
from app.execution.validator import validate
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore
from app.opportunity.detector import OpportunityDetector
from app.simulation.paper_trader import PaperTrader, TradeStatus
from app.simulation.portfolios import VirtualPortfolio
from app.simulation.position_tracker import OpenPositionTracker

EXECUTED_STATUSES = (TradeStatus.SIMULATED_EXECUTED, TradeStatus.PARTIAL_FILL, TradeStatus.EMERGENCY_UNWIND, TradeStatus.TIME_STOP_EXIT)


@dataclass(slots=True)
class MarketEvent:
    """One quote update to feed into the replay's private MarketDataStore.

    `offset_seconds` is relative to replay start, not an absolute historical
    epoch: the engines this replays (cross_exchange, basis, ...) read the
    real wall clock internally (time.time()) for staleness checks, so a
    literal historical timestamp from days ago would make every quote look
    stale on arrival. Small, ascending offsets (0.0, 0.1, 0.2, ...) simulate
    a session unfolding in real time instead.
    """

    exchange: str
    market: MarketType
    symbol: str
    bid: float
    ask: float
    bid_quantity: float
    ask_quantity: float
    offset_seconds: float = 0.0


@dataclass(slots=True)
class ReplayTradeRecord:
    strategy: str
    symbol: str
    status: str
    net_profit_usd: float


@dataclass(slots=True)
class ReplayResult:
    opportunities_detected: int = 0
    trades: list[ReplayTradeRecord] = field(default_factory=list)

    @property
    def trades_executed(self) -> int:
        return sum(1 for t in self.trades if t.status in EXECUTED_STATUSES)

    @property
    def net_profit_usd(self) -> float:
        return round(sum(t.net_profit_usd for t in self.trades if t.status in EXECUTED_STATUSES), 10)

    @property
    def trades_won(self) -> int:
        return sum(1 for t in self.trades if t.status in EXECUTED_STATUSES and t.net_profit_usd > 0)

    @property
    def trades_lost(self) -> int:
        return sum(1 for t in self.trades if t.status in EXECUTED_STATUSES and t.net_profit_usd < 0)

    def as_status_tuple(self) -> tuple[tuple[str, str, str, float], ...]:
        """A hashable/comparable fingerprint of the run, for determinism tests."""
        return tuple((t.strategy, t.symbol, t.status, round(t.net_profit_usd, 10)) for t in self.trades)


async def run_replay(
    events: list[MarketEvent],
    build_engines: "callable[[MarketDataStore], list[ArbitrageEngine]]",
    portfolio: VirtualPortfolio,
    *,
    seed: int,
    latency_profile: LatencyProfile = DEFAULT_PROFILE,
    replay_start: float,
) -> ReplayResult:
    """Replay `events` in order through a private store/detector/trader.

    `replay_start` is the wall-clock time (time.time()) this replay's event
    offsets are anchored to — callers pass it explicitly (rather than this
    function calling time.time() itself) so a caller running two replays
    back-to-back for a determinism test can anchor both to the same instant
    if they want strictly identical `now` values passed into validate()/simulate().
    """
    store = MarketDataStore()
    detector = OpportunityDetector(build_engines(store))
    position_tracker = OpenPositionTracker()
    trader = PaperTrader(rng=random.Random(seed), latency_profile=latency_profile)

    result = ReplayResult()

    for event in events:
        now = replay_start + event.offset_seconds
        store.update_quote(
            NormalizedQuote(
                exchange=event.exchange,
                market=event.market,
                symbol=event.symbol,
                bid=event.bid,
                ask=event.ask,
                bid_quantity=event.bid_quantity,
                ask_quantity=event.ask_quantity,
                exchange_timestamp=now,
                received_at=now,
            )
        )

        opportunities = await detector.scan_once()
        result.opportunities_detected += len(opportunities)

        for opp in opportunities:
            validation = validate(opp, position_tracker, now=now)
            if not validation.approved:
                continue

            if opp.holding_period_seconds is not None and opp.legs:
                position_key = (opp.strategy, opp.legs[0].get("exchange"), opp.symbol)
                position_tracker.open_position(position_key, now, opp.holding_period_seconds)

            outcome = trader.determine_outcome(opp)
            trade = trader.simulate(opp, portfolio, outcome, now=now)
            result.trades.append(
                ReplayTradeRecord(
                    strategy=str(opp.strategy), symbol=opp.symbol, status=trade.status.value, net_profit_usd=trade.net_profit_usd
                )
            )

    return result
