"""ALTCOIN SCANNER — CROSS-EXCHANGE OPPORTUNITY MONITORING, FULL DYNAMIC
UNIVERSE (user directive, 2026-08-23, extended to the full universe
2026-08-24 — "INVENTORY MANAGER V2").

A completely separate process from main.py — does NOT import main, does
NOT import app.orchestration.global_allocator, app.orchestration.control,
or app.execution.live_arbitrage_executor (see app/scanner/__init__.py and
tests/test_scanner_isolation.py, enforced mechanically). MASTER stays in
Shadow Mode for everything this process observes; real_orders_placed is
always 0 and no code path here could change that. This process can be
stopped, crashed, or deleted entirely without touching paper or live
trading state in any way.

TWO-STAGE SCANNER, covering the full dynamic Binance∩Bybit Spot USDT
universe (app.execution.live_universe — no fixed watchlist, no hardcoded
symbol list, new listings/delistings tracked automatically):

  STAGE A (app.scanner.fast_discovery): two bulk-ticker HTTP calls (one
  per exchange, every symbol at once) cheaply rank the WHOLE universe by
  raw cross-exchange spread, with a small momentum-driven top-up for
  exploratory coverage. No per-symbol network I/O.

  STAGE B (app.scanner.cross_exchange_scanner.scan_symbol, unchanged,
  already-validated Phase 2F/3A math): real fees, real depth, real
  slippage, real dual-leg latency — run ONLY on STAGE A's promoted
  candidates (bounded concurrency), never on the full universe. This is
  what keeps cycle time roughly constant as the universe grows instead
  of scaling linearly with it (an earlier, unconditional full-universe
  scan measured 4m40s live for ~240 symbols — see
  app.execution.live_ranker's own note).

Every STAGE B result is persisted to altcoin_scan_observations exactly
as before ("persiste intelligemment" — only validated candidates, never
every ticker on every tick). A single-row FullUniverseScanStatusRecord
is upserted every cycle so the engine process (a separate systemd
service, no shared memory with this one) can read live STAGE A/B
counters for GET /live/full-universe-discovery.

Run with: python altcoin_scanner.py
Deployed as its own systemd unit (robotcripto-altcoin-scanner.service),
independent of robotcripto-engine.service and robotcripto-shadow.service.
"""

import asyncio
import logging
import time
from datetime import UTC, datetime

from app.config.settings import get_settings
from app.database.repository import (
    LIVE_MARKET_EXCHANGES,
    save_altcoin_scan_observation,
    upsert_full_universe_scan_status,
    upsert_missed_opportunity_summaries,
)
from app.database.session import async_session_factory
from app.execution.binance_account_client import BinanceAccountClient
from app.execution.bybit_client import BybitClient
from app.execution.live_universe import live_universe_builder
from app.reporting.dual_leg_edge import recommend_safety_margin_usd
from app.scanner.continuity_tracker import ContinuityTracker
from app.scanner.cross_exchange_scanner import scan_symbol
from app.scanner.fast_discovery import discover_candidates, parse_binance_bulk_tickers, parse_bybit_bulk_tickers
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher
from app.scanner.missed_opportunity_tracker import EdgeDisappearanceTracker, MissedOpportunityTracker, classify_miss

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("altcoin_scanner")

# LUNC stays available as the benchmark reference (Phase 2E/2F's
# validated case) simply by being part of the real Binance∩Bybit
# universe — no special-casing needed, no hardcoded list.
STAGE_B_CONCURRENCY = 8


async def _run_stage_b(fetcher: MultiExchangeSnapshotFetcher, symbols: list[str]) -> dict[str, list]:
    """Bounded-concurrency scan_symbol calls — real network I/O across
    up to full_universe_scan_max_stage_b_per_cycle symbols would be slow
    run sequentially; a semaphore keeps concurrent requests from
    hammering either exchange's rate limits while still cutting wall
    time roughly by STAGE_B_CONCURRENCY."""
    semaphore = asyncio.Semaphore(STAGE_B_CONCURRENCY)
    results: dict[str, list] = {}

    async def _one(symbol: str) -> None:
        async with semaphore:
            try:
                results[symbol] = await scan_symbol(fetcher, symbol)
            except Exception:
                logger.exception("scan failed for %s — skipping this symbol this cycle", symbol)

    await asyncio.gather(*(_one(s) for s in symbols))
    return results


async def run_one_cycle(
    fetcher: MultiExchangeSnapshotFetcher,
    binance_read: BinanceAccountClient,
    bybit_read: BybitClient,
    tracker: ContinuityTracker,
    edge_disappearance: EdgeDisappearanceTracker,
    missed: MissedOpportunityTracker,
) -> int:
    settings = get_settings()
    cycle_start = time.time()
    now = datetime.now(UTC).replace(tzinfo=None)

    universe = await live_universe_builder.get_universe()

    binance_tickers = {}
    bybit_tickers = {}
    try:
        raw_binance = await binance_read.get_all_tickers_24hr()
        binance_tickers = parse_binance_bulk_tickers(raw_binance)
    except Exception:
        logger.exception("STAGE A: Binance bulk ticker fetch failed — this cycle's discovery will find nothing")
    try:
        raw_bybit = await bybit_read.get_all_tickers()
        bybit_tickers = parse_bybit_bulk_tickers(raw_bybit)
    except Exception:
        logger.exception("STAGE A: Bybit bulk ticker fetch failed — this cycle's discovery will find nothing")

    discovery = discover_candidates(
        universe.common_symbols,
        binance_tickers,
        bybit_tickers,
        min_raw_spread_pct=settings.full_universe_scan_min_raw_spread_pct,
        max_candidates=settings.full_universe_scan_max_stage_b_per_cycle,
        momentum_top_up=settings.full_universe_scan_momentum_top_up,
    )
    stage_b_symbols = sorted({c.symbol for c in discovery.candidates})
    stage_b_results = await _run_stage_b(fetcher, stage_b_symbols)  # network I/O done before opening the DB session

    # Data-derived safety margin (item 5) from THIS cycle's own net-profit
    # distribution — same established methodology as
    # app.reporting.dual_leg_edge.recommend_safety_margin_usd (1
    # population stdev), computed fresh each cycle, never a fitted or
    # invented constant.
    all_quotes = [dq for direction_quotes in stage_b_results.values() for dq in direction_quotes]
    safety_margin_usd = recommend_safety_margin_usd([dq.quote.net_profit_usd for dq in all_quotes])

    total = 0
    net_positive_live = 0
    async with async_session_factory() as session:
        for symbol, direction_quotes in stage_b_results.items():
            for dq in direction_quotes:
                is_positive = dq.quote.executable and dq.quote.net_profit_usd > 0
                is_live = dq.buy_exchange in LIVE_MARKET_EXCHANGES and dq.sell_exchange in LIVE_MARKET_EXCHANGES
                if is_positive and is_live:
                    net_positive_live += 1
                status = tracker.observe(dq.symbol, dq.buy_exchange, dq.sell_exchange, is_positive)
                persistence = tracker.current_persistence_seconds(dq.symbol, dq.buy_exchange, dq.sell_exchange)
                try:
                    await save_altcoin_scan_observation(session, dq, observed_at=now, continuity_status=status, persistence_seconds=persistence)
                    total += 1
                except Exception:
                    logger.exception("failed to persist observation for %s %s->%s", dq.symbol, dq.buy_exchange, dq.sell_exchange)

                # Missed-opportunity classification is LIVE-scope only
                # (item 2) — an OKX-involving quote must never contribute
                # to a count that could influence the Inventory Manager.
                if not is_live:
                    continue
                disappeared_profit = edge_disappearance.observe(dq.symbol, dq.buy_exchange, dq.sell_exchange, is_positive, dq.quote.net_profit_usd)
                if disappeared_profit is not None:
                    missed.record("EDGE_DISAPPEARED", disappeared_profit)
                    continue  # this tick's own cause (if any) is a separate, independent event from the one that just disappeared
                cause, theoretical_profit = classify_miss(dq.quote, safety_margin_usd=safety_margin_usd)
                if cause is not None:
                    missed.record(cause, theoretical_profit)

        cycle_duration = time.time() - cycle_start
        try:
            await upsert_full_universe_scan_status(
                session,
                updated_at=now,
                common_pairs_count=len(universe.common_symbols),
                pairs_fast_scanned=discovery.fast_scanned_count,
                pairs_raw_spread_stage_a=discovery.raw_edge_count,
                pairs_deep_validated=len(stage_b_symbols),
                pairs_net_positive_stage_b_live=net_positive_live,
                cycle_duration_seconds=cycle_duration,
            )
        except Exception:
            logger.exception("failed to upsert full_universe_scan_status")

        try:
            snapshot = missed.snapshot()
            await upsert_missed_opportunity_summaries(
                session,
                {cause: (acc.count, acc.theoretical_profit_usd_total) for cause, acc in snapshot.items()},
                updated_at=now,
            )
        except Exception:
            logger.exception("failed to upsert missed_opportunity_summary")

        await session.commit()
    return total


async def main() -> None:
    logger.info("altcoin_scanner starting — SHADOW MODE ONLY, no executor ever called, real_orders_placed stays 0")
    logger.info("full dynamic Binance∩Bybit universe, two-stage scan — no fixed watchlist")
    settings = get_settings()
    binance_read = BinanceAccountClient()
    bybit_read = BybitClient()
    fetcher = MultiExchangeSnapshotFetcher(binance=binance_read, bybit=bybit_read)
    tracker = ContinuityTracker()
    edge_disappearance = EdgeDisappearanceTracker()
    missed = MissedOpportunityTracker()

    while True:
        try:
            count = await run_one_cycle(fetcher, binance_read, bybit_read, tracker, edge_disappearance, missed)
            logger.info("scan cycle complete: %d direction-observations persisted", count)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("altcoin_scanner cycle failed — will retry next poll, real engines are unaffected")

        await asyncio.sleep(settings.full_universe_scan_interval_seconds)


if __name__ == "__main__":
    asyncio.run(main())
