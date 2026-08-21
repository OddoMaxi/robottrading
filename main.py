"""Entrypoint — boots the FastAPI app together with the collectors, funding
pollers, and the detection/paper-trading loop as background asyncio tasks.
"""

import asyncio
import logging
import time
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.routes import router
from app.collectors.binance.basis_futures import poll_binance_delivery_futures
from app.collectors.binance.collector import BinanceCollector
from app.collectors.binance.depth_collector import BinanceDepthCollector
from app.collectors.binance.funding import poll_binance_funding
from app.collectors.bybit.collector import BybitCollector
from app.collectors.bybit.depth_collector import BybitDepthCollector
from app.collectors.bybit.funding import poll_bybit_funding
from app.collectors.okx.collector import OkxCollector
from app.collectors.okx.depth_collector import OkxDepthCollector
from app.collectors.okx.funding import poll_okx_funding
from app.config.constants import (
    CROSS_EXCHANGE_ASSETS,
    DELIVERY_FUTURES_ASSETS,
    MarketType,
    PRIORITY_EXCHANGES,
    STABLECOIN_PAIRS,
    TRIANGULAR_CROSS_PAIRS,
)
from app.config.settings import get_settings
from app.database.repository import (
    close_opportunity_tracking,
    close_orphaned_opportunity_tracking,
    create_all_tables,
    get_or_create_exchange,
    get_or_create_portfolio,
    save_opportunity,
    save_price_snapshots,
    save_simulated_trade,
    update_opportunity_tracking,
)
from app.database.session import async_session_factory
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.engines.stablecoin import StablecoinArbitrageEngine
from app.engines.triangular import TriangularArbitrageEngine
from app.execution.validator import validate
from app.market_data.store import market_data_store
from app.market_data.symbol_discovery import DiscoveredUniverse, discover_symbol_universe
from app.execution.binance_testnet_client import BinanceTestnetClient
from app.opportunity.detector import OpportunityDetector
from app.opportunity.tracker import OpportunityTracker
from app.reporting.micro_live_readiness import build_micro_live_readiness
from app.reporting.shadow_live import build_shadow_live_status
from app.risk.risk_engine import risk_engine
from app.simulation.ledger_integrity import check_ledger_integrity
from app.simulation.time_stop import force_exit_overdue_positions
from app.simulation.paper_trader import PaperTrader
from app.simulation.portfolios import build_default_portfolios
from app.simulation.position_tracker import OpenPositionTracker
from app.simulation.state_recovery import rebuild_portfolio_state

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

# Opportunity Expansion spec, Step 1 (user directive, 2026-08-21) — these two
# are now the STATIC FALLBACK only, used if live discovery (below) fails or
# comes back implausibly small. The values collectors/engines actually run
# with are computed at startup by _resolve_symbol_universe().
SPOT_SYMBOLS = sorted({f"{a}/USDT" for a in CROSS_EXCHANGE_ASSETS} | set(STABLECOIN_PAIRS) | set(TRIANGULAR_CROSS_PAIRS))
CHART_SYMBOLS = [f"{a}/USDT" for a in CROSS_EXCHANGE_ASSETS]

# Confirmed live, 2026-08-21 (repeated, consistent WS subscribe rejections
# across 5 separate process restarts): these TRIANGULAR_CROSS_PAIRS symbols
# are not listed on Bybit at all. Every static SPOT_SYMBOLS entry used to be
# pushed identically to all 3 exchanges regardless of whether it's real
# there — this wasted ~19% of Bybit's subscription slots on symbols it will
# never return a quote for. Hand-verified exclusion rather than a 4th
# discovery call (discover_symbol_universe only covers X/USDT pairs, not
# these BTC/FDUSD-quoted triangular-only ones) — a fuller "discover every
# quote asset, not just USDT" system is a legitimate future step, not done
# tonight.
BYBIT_UNLISTED_SYMBOLS = {"BNB/BTC", "BNB/FDUSD", "BTC/FDUSD", "ETH/FDUSD", "FDUSD/USDC", "FDUSD/USDT", "SOL/FDUSD", "XRP/FDUSD"}
# Below this many assets confirmed live on 2+ exchanges, a discovery result
# is more likely a partial outage/bug than a genuine "the market shrank"
# signal — distrust it and fall back to the static list rather than run
# the engine on a suspiciously tiny universe.
MIN_DISCOVERED_ASSETS = 5
_last_discovered_universe: DiscoveredUniverse | None = None  # set by _resolve_symbol_universe(), read by /market/symbol-universe


async def _resolve_symbol_universe() -> tuple[list[str], list[str], dict[str, list[str]]]:
    """Live REST discovery (app.market_data.symbol_discovery) replaces the
    hand-verified-once CROSS_EXCHANGE_ASSETS with what each exchange
    actually lists, live, right now — self-correcting on every restart
    instead of silently drifting stale between manual re-checks. Returns
    (effective_assets, effective_chart_symbols, per_exchange_spot_symbols).
    """
    global _last_discovered_universe
    universe = await discover_symbol_universe()
    _last_discovered_universe = universe

    if universe.degraded or len(universe.assets_on_2_or_more_exchanges) < MIN_DISCOVERED_ASSETS:
        logger.warning(
            "symbol discovery degraded or implausibly small (%d assets, degraded=%s) — falling back to the static %d-asset list",
            len(universe.assets_on_2_or_more_exchanges),
            universe.degraded,
            len(CROSS_EXCHANGE_ASSETS),
        )
        effective_assets = CROSS_EXCHANGE_ASSETS
    else:
        added = sorted(set(universe.assets_on_2_or_more_exchanges) - set(CROSS_EXCHANGE_ASSETS))
        removed = sorted(set(CROSS_EXCHANGE_ASSETS) - set(universe.assets_on_2_or_more_exchanges))
        logger.warning(
            "symbol discovery: %d assets confirmed live on 2+ exchanges (static list had %d) — added=%s removed=%s",
            len(universe.assets_on_2_or_more_exchanges),
            len(CROSS_EXCHANGE_ASSETS),
            added,
            removed,
        )
        effective_assets = universe.assets_on_2_or_more_exchanges

    effective_chart_symbols = [f"{a}/USDT" for a in effective_assets]
    base_spot = sorted(set(effective_chart_symbols) | set(STABLECOIN_PAIRS) | set(TRIANGULAR_CROSS_PAIRS))
    per_exchange_spot_symbols = {
        "binance": base_spot,
        "okx": base_spot,
        "bybit": [s for s in base_spot if s not in BYBIT_UNLISTED_SYMBOLS],
    }
    return effective_assets, effective_chart_symbols, per_exchange_spot_symbols
# Event-driven detection: react to a new tick almost immediately instead of
# polling every few seconds — a real spread observed on 2026-08-19 opened
# and closed within ~15 seconds, which a fixed 3s poll would mostly miss.
# MIN bounds the scan rate during a burst of ticks (protects the DB write
# rate); MAX is the fallback so the loop still ticks even with no new data.
MIN_SCAN_INTERVAL_SECONDS = 0.25
MAX_SCAN_WAIT_SECONDS = 3.0

portfolios = build_default_portfolios()
paper_trader = PaperTrader()
# Basis/Funding positions tie up capital for days once opened — without
# this, the same persistent condition re-detected every scan (several
# times a second) gets paper-traded as a brand new trade each time. Found
# in production: a $1,000 portfolio showed $52,614 of cumulative simulated
# basis profit from 21,640 paper trades of the *same* underlying position.
position_tracker = OpenPositionTracker()
# Continuous Execution spec, sections 3, 5-11 — a spread persisting for
# several scans is one opportunity with many observations, not a new row
# each time. Without this, the opportunities table records the same
# economic event thousands of times over (event-driven detection can
# re-fire several times a second).
opportunity_tracker = OpportunityTracker()
# Populated once by lifespan() at startup, mutated in place (never
# reassigned) — module-level so both detection_loop and the /ledger/*
# endpoints below can look up each portfolio's DB id.
portfolio_ids: dict[str, int] = {}
background_tasks: list[asyncio.Task] = []

# Ledger Integrity (Reality Engine spec, sections 30-31) — cheap enough to
# run often, but not worth a DB round-trip per scan when scans can fire
# several times a second; once a minute is plenty to catch a real drift
# between the live portfolios and the DB ledger long before it matters.
LEDGER_CHECK_INTERVAL_SECONDS = 60.0
_last_ledger_check_at = 0.0


async def detection_loop(detector: OpportunityDetector, portfolio_ids: dict[str, int]) -> None:
    global _last_ledger_check_at
    while True:
        try:
            opportunities = await detector.scan_once()
            quotes = [
                q
                for exchange in PRIORITY_EXCHANGES
                for symbol in CHART_SYMBOLS
                if (q := market_data_store.get_quote(exchange, MarketType.SPOT, symbol)) is not None
            ]
            scan_time = time.time()
            async with async_session_factory() as session:
                if quotes:
                    await save_price_snapshots(session, quotes)
                for opp in opportunities:
                    # Continuous Execution spec, sections 12-15 — the single
                    # gate deciding whether this is even worth attempting,
                    # with a traceable reason when it isn't (replaces the
                    # old inline classification + position-open checks).
                    validation = validate(opp, position_tracker, now=scan_time)
                    opp.rejection_reason = validation.reason.value if validation.reason else None

                    observation = opportunity_tracker.observe(opp, now=scan_time)
                    if observation.is_new:
                        await save_opportunity(session, opp)
                    else:
                        await update_opportunity_tracking(session, observation.tracked, opp, rejection_reason=opp.rejection_reason)

                    # Kill switch (spec section 61) — stops new executions
                    # immediately, in simulation too. Detection, tracking,
                    # and persistence above are unaffected; only capital
                    # allocation halts.
                    if validation.approved and not risk_engine.kill_switch_engaged:
                        now = scan_time
                        # A held position (Basis/Funding) ties up its
                        # (strategy, exchange, symbol) until it would
                        # actually have closed — validate() already checked
                        # this wasn't already open, so it's safe to mark it now.
                        if opp.holding_period_seconds is not None and opp.legs:
                            position_key = (opp.strategy, opp.legs[0].get("exchange"), opp.symbol)
                            position_tracker.open_position(position_key, now, opp.holding_period_seconds)

                        # Sampled once per opportunity — a maker leg's fill
                        # outcome is a property of the market, not of which
                        # virtual portfolio happens to be replaying it.
                        outcome = paper_trader.determine_outcome(opp)
                        for portfolio in portfolios:
                            trade = paper_trader.simulate(opp, portfolio, outcome, now=now)
                            await save_simulated_trade(session, trade, opp.id, portfolio_ids[portfolio.name])

                # FAST TRADING ONLY (user directive, 2026-08-21) — 30-minute
                # hard stop. Cheap when nothing is overdue (a single
                # in-memory dict scan per portfolio, no DB query), so this
                # runs every scan rather than being throttled like the
                # ledger check below.
                for portfolio in portfolios:
                    forced_count = await force_exit_overdue_positions(session, portfolio, portfolio_ids[portfolio.name], now=scan_time)
                    if forced_count:
                        logger.warning("TIME STOP: forced %d overdue position(s) out of portfolio %s", forced_count, portfolio.name)

                # Signals that stopped being observed this scan — close them
                # out rather than leaving them ACTIVE forever. A later
                # re-emergence of the same key is then correctly treated as
                # a brand new opportunity (spec section 11).
                for tracked in opportunity_tracker.expire_stale(now=scan_time):
                    await close_opportunity_tracking(session, tracked, closed_at=scan_time)
                await session.commit()

                # Ledger Integrity (spec sections 30-31, 10) — a violation
                # here means the live portfolios have drifted out of sync
                # with the DB ledger (a write that silently failed, a
                # restart-recovery inconsistency): stop all new executions
                # immediately rather than keep compounding an accounting
                # error. Detection/observation keep running.
                if scan_time - _last_ledger_check_at >= LEDGER_CHECK_INTERVAL_SECONDS:
                    _last_ledger_check_at = scan_time
                    for portfolio in portfolios:
                        check = await check_ledger_integrity(session, portfolio, portfolio_ids[portfolio.name], now=scan_time)
                        if not check.reconciled and not risk_engine.kill_switch_engaged:
                            logger.critical(
                                "LEDGER INTEGRITY VIOLATION on portfolio %s: %s — engaging kill switch",
                                check.portfolio_name,
                                "; ".join(check.violations),
                            )
                            risk_engine.engage_kill_switch(f"ledger integrity violation: {'; '.join(check.violations)}")
            if opportunities:
                logger.info("scan: %d opportunities detected", len(opportunities))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("detection loop iteration failed")
        await market_data_store.wait_for_update(timeout=MAX_SCAN_WAIT_SECONDS)
        await asyncio.sleep(MIN_SCAN_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_tables()

    async with async_session_factory() as session:
        for exchange in PRIORITY_EXCHANGES:
            await get_or_create_exchange(session, exchange, exchange.capitalize())
        for portfolio in portfolios:
            record = await get_or_create_portfolio(session, portfolio.name, portfolio.initial_capital_usd)
            portfolio_ids[portfolio.name] = record.id
        await session.commit()

        # Urgent audit fix — position/capital lock state is in-memory only
        # and a restart otherwise forgets every currently-open position,
        # letting the engine re-allocate capital that's actually still
        # committed for days or weeks (Basis/Funding). Must run before any
        # collector/detection task starts.
        await rebuild_portfolio_state(session, portfolios, portfolio_ids, position_tracker)

        # Same restart-amnesia bug class, applied to opportunity tracking —
        # the in-memory OpportunityTracker built above starts empty, so any
        # row still 'detected'/'active' in the DB from the previous process
        # would otherwise never get closed out. Must also run before any
        # collector/detection task starts.
        orphaned_count = await close_orphaned_opportunity_tracking(session)
        await session.commit()
        if orphaned_count:
            logger.warning("closed %d orphaned opportunity-tracking rows left open by a previous process", orphaned_count)

    # Opportunity Expansion spec, Step 1 (user directive, 2026-08-21) — live
    # discovery replaces the static, hand-verified-once symbol lists with
    # what each exchange actually lists right now. Reassigning the module
    # globals (rather than only using locals here) means detection_loop's
    # own CHART_SYMBOLS reference — read fresh from the module namespace on
    # every scan — picks up the discovered set too, with no other code
    # changed there.
    global CHART_SYMBOLS
    effective_assets, CHART_SYMBOLS, per_exchange_spot_symbols = await _resolve_symbol_universe()

    collectors = [
        BinanceCollector(per_exchange_spot_symbols["binance"]),
        OkxCollector(per_exchange_spot_symbols["okx"]),
        BybitCollector(per_exchange_spot_symbols["bybit"]),
        # Opportunity Expansion spec, Step 2 (user directive, 2026-08-21) —
        # real multi-level depth on all 3 priority exchanges, not Binance
        # only. Purely additive on every exchange: each top-of-book
        # collector above is untouched, so a bug in any one depth collector
        # can't take down the quotes every engine already depends on.
        # app.engines._shared._resolve_ask_levels/_resolve_bid_levels
        # already read whichever exchange's order book the store has — no
        # engine-side change needed, they just had nothing to read for
        # OKX/Bybit before this.
        BinanceDepthCollector(CHART_SYMBOLS),
        OkxDepthCollector(CHART_SYMBOLS),
        BybitDepthCollector(CHART_SYMBOLS),
    ]
    for collector in collectors:
        task_name = f"collector:{collector.exchange}:{type(collector).__name__}"
        background_tasks.append(asyncio.create_task(collector.run(market_data_store), name=task_name))

    background_tasks.append(asyncio.create_task(poll_binance_funding(market_data_store, CROSS_EXCHANGE_ASSETS), name="funding:binance"))
    background_tasks.append(asyncio.create_task(poll_okx_funding(market_data_store, CROSS_EXCHANGE_ASSETS), name="funding:okx"))
    background_tasks.append(asyncio.create_task(poll_bybit_funding(market_data_store, CROSS_EXCHANGE_ASSETS), name="funding:bybit"))
    background_tasks.append(
        asyncio.create_task(poll_binance_delivery_futures(market_data_store, DELIVERY_FUTURES_ASSETS), name="basis:binance")
    )

    # FAST TRADING ONLY (user directive, 2026-08-21) — Basis and Funding are
    # deliberately excluded. Both naturally hold for hours-to-weeks (basis
    # converges at the future's expiry; funding accrues per 8h cycle), which
    # is the opposite of this engine's purpose: fast profit, fast capital
    # release, fast re-entry. A single basis position was found to have
    # locked ~$5,015 of a $5,196 portfolio until 2026-09-25. The engine
    # classes and their collectors are left in place (disabled, not
    # deleted) in case Carry Mode becomes its own deliberate product
    # decision later — this is a strategy-set change, not a data-loss one.
    engines = [
        StablecoinArbitrageEngine(),
        # Opportunity Expansion spec, Step 1 — the live-discovered universe,
        # not the static CROSS_EXCHANGE_ASSETS (see _resolve_symbol_universe).
        CrossExchangeArbitrageEngine(assets=effective_assets),
        *(TriangularArbitrageEngine(exchange=exchange) for exchange in PRIORITY_EXCHANGES),
    ]
    detector = OpportunityDetector(engines)
    background_tasks.append(asyncio.create_task(detection_loop(detector, portfolio_ids), name="detection_loop"))

    logger.info("startup complete: %d background tasks running", len(background_tasks))
    yield

    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(router)


@app.get("/capital/pool")
async def capital_pool() -> list[dict]:
    """Continuous Execution spec, sections 16-17 — the engine's live,
    authoritative capital state per portfolio (not the DB-reconstructed
    approximation the dashboard uses, which lags slightly behind)."""
    now = time.time()
    return [
        {
            "portfolio": portfolio.name,
            "total_capital_usd": portfolio.current_value_usd,
            "available_usd": portfolio.available_usd(now),
            "engaged_usd": portfolio.current_value_usd - portfolio.available_usd(now),
            "open_position_count": portfolio.open_position_count(now),
        }
        for portfolio in portfolios
    ]


@app.get("/ledger/integrity")
async def ledger_integrity() -> list[dict]:
    """Reality Engine spec, sections 30-31 — on-demand version of the same
    check the detection loop already runs every LEDGER_CHECK_INTERVAL_SECONDS
    and engages the kill switch on: live portfolio equity vs. what the DB
    trade ledger itself reconstructs it as, for every portfolio right now."""
    now = time.time()
    results = []
    async with async_session_factory() as session:
        for portfolio in portfolios:
            check = await check_ledger_integrity(session, portfolio, portfolio_ids[portfolio.name], now=now)
            results.append(
                {
                    "portfolio": check.portfolio_name,
                    "equity_usd": check.equity_usd,
                    "available_usd": check.available_usd,
                    "db_reconstructed_equity_usd": check.db_reconstructed_equity_usd,
                    "reconciled": check.reconciled,
                    "violations": check.violations,
                }
            )
    return results


@app.get("/shadow-live/status")
async def shadow_live_status() -> dict:
    """Reality Engine spec, sections 55-56 — confirms in one auditable
    place that this is a genuine shadow of live trading: real market data,
    zero real orders placed, and a breakdown of what the engine is
    currently deciding for every signal on its radar right now."""
    async with async_session_factory() as session:
        status = await build_shadow_live_status(session, risk_engine)
    return {
        "mode": status.mode,
        "real_orders_placed": status.real_orders_placed,
        "robot_health": status.robot_status.health.value,
        "exchanges_connected": status.robot_status.exchanges_connected,
        "last_opportunity_age_seconds": status.robot_status.last_opportunity_age_seconds,
        "kill_switch_engaged": status.kill_switch_engaged,
        "kill_switch_reason": status.kill_switch_reason,
        "signals_on_radar": status.signals_on_radar,
        "approved_on_radar": status.approved_on_radar,
        "rejection_breakdown": status.rejection_breakdown,
    }


@app.get("/market/symbol-universe")
async def symbol_universe() -> dict:
    """Opportunity Expansion spec, Step 1 — the result of the live symbol
    discovery run at this process's startup (app.market_data.symbol_discovery),
    for the same reason /shadow-live/status exists: an auditable place to
    see exactly what the engine decided rather than trust it happened."""
    if _last_discovered_universe is None:
        return {"available": False, "detail": "discovery has not run yet (still starting up)"}
    universe = _last_discovered_universe
    return {
        "available": True,
        "degraded": universe.degraded,
        "assets_on_2_or_more_exchanges": universe.assets_on_2_or_more_exchanges,
        "asset_count": len(universe.assets_on_2_or_more_exchanges),
        "per_exchange": {
            exchange: {"reachable": result.reachable, "symbols_above_liquidity_floor": len(result.quote_volume_by_symbol)}
            for exchange, result in universe.per_exchange.items()
        },
        "static_fallback_asset_count": len(CROSS_EXCHANGE_ASSETS),
        "bybit_confirmed_unlisted_symbols": sorted(BYBIT_UNLISTED_SYMBOLS),
    }


_binance_testnet_client = BinanceTestnetClient()


@app.get("/micro-live/readiness")
async def micro_live_readiness() -> dict:
    """Reality Engine spec, sections 59-60 — composes every safety/quality
    signal this system already computes (Ledger Integrity, Capital Pool,
    Reality Capture, Performance Metrics, Stress Testing, Binance Testnet
    connectivity) into one checklist and a single READY_FOR_CONTROLLED_TEST
    / NOT_READY verdict. Read-only: never places an order, testnet or
    otherwise — see app.execution.binance_testnet_client's own docstring."""
    async with async_session_factory() as session:
        report = await build_micro_live_readiness(session, portfolios, portfolio_ids, risk_engine, _binance_testnet_client)
    return {
        "verdict": report.verdict.value,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in report.checks],
    }


if __name__ == "__main__":
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
