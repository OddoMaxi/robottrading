"""ALTCOIN SCANNER — CROSS-EXCHANGE OPPORTUNITY MONITORING (user directive,
2026-08-23).

A completely separate process from main.py — does NOT import main, does
NOT import app.orchestration.global_allocator, app.orchestration.control,
or app.execution.live_arbitrage_executor (see app/scanner/__init__.py and
tests/test_scanner_isolation.py, enforced mechanically). MASTER stays in
Shadow Mode for everything this process observes; real_orders_placed is
always 0 and no code path here could change that. This process can be
stopped, crashed, or deleted entirely without touching paper or live
trading state in any way.

Polls live REST market data (Binance, Bybit, OKX — public endpoints plus
the existing read-only account clients' real fee-rate endpoints) for a
fixed watchlist, evaluates every ordered exchange-pair direction via
app.execution.dual_leg_quote (already-validated math from Phase 2F/3A),
and persists every observation to altcoin_scan_observations.

Run with: python altcoin_scanner.py
Deployed as its own systemd unit (robotcripto-altcoin-scanner.service),
independent of robotcripto-engine.service and robotcripto-shadow.service.
"""

import asyncio
import logging
from datetime import UTC, datetime

from app.database.repository import save_altcoin_scan_observation
from app.database.session import async_session_factory
from app.scanner.continuity_tracker import ContinuityTracker
from app.scanner.cross_exchange_scanner import scan_symbol
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("altcoin_scanner")

SCAN_INTERVAL_SECONDS = 10.0

# Priority order per the user's own directive, 2026-08-23. LUNC stays as
# the benchmark reference (Phase 2E/2F's validated case), even though its
# own cross-exchange listing is currently thin on Bybit (see the Phase 3A
# conversation) — comparing everything else against it is the whole point.
WATCHLIST = [
    "ZRO/USDT",
    "STX/USDT",
    "PUMP/USDT",
    "TRAC/USDT",
    "MORPHO/USDT",
    "LUNC/USDT",  # benchmark
    "PEPE/USDT",
    "DOGE/USDT",
    "SHIB/USDT",
    "BONK/USDT",
    "XRP/USDT",
    "ZEC/USDT",
]


async def run_one_cycle(fetcher: MultiExchangeSnapshotFetcher, tracker: ContinuityTracker) -> int:
    now = datetime.now(UTC).replace(tzinfo=None)
    total = 0
    async with async_session_factory() as session:
        for symbol in WATCHLIST:
            try:
                direction_quotes = await scan_symbol(fetcher, symbol)
            except Exception:
                logger.exception("scan failed for %s — skipping this symbol this cycle", symbol)
                continue

            for dq in direction_quotes:
                is_positive = dq.quote.executable and dq.quote.net_profit_usd > 0
                status = tracker.observe(dq.symbol, dq.buy_exchange, dq.sell_exchange, is_positive)
                persistence = tracker.current_persistence_seconds(dq.symbol, dq.buy_exchange, dq.sell_exchange)
                try:
                    await save_altcoin_scan_observation(session, dq, observed_at=now, continuity_status=status, persistence_seconds=persistence)
                    total += 1
                except Exception:
                    logger.exception("failed to persist observation for %s %s->%s", dq.symbol, dq.buy_exchange, dq.sell_exchange)
        await session.commit()
    return total


async def main() -> None:
    logger.info("altcoin_scanner starting — SHADOW MODE ONLY, no executor ever called, real_orders_placed stays 0")
    logger.info("watchlist (%d symbols): %s", len(WATCHLIST), ", ".join(WATCHLIST))
    fetcher = MultiExchangeSnapshotFetcher()
    tracker = ContinuityTracker()

    while True:
        try:
            count = await run_one_cycle(fetcher, tracker)
            logger.info("scan cycle complete: %d direction-observations persisted", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("altcoin_scanner cycle failed — will retry next poll, real engines are unaffected")

        await asyncio.sleep(SCAN_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
