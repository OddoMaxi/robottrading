"""Entrypoint — boots the FastAPI app together with the collectors, funding
pollers, and the detection/paper-trading loop as background asyncio tasks.
"""

import asyncio
import logging
import random
import time
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime

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
    save_cex_scan_event,
    save_dex_trade_attempt,
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
from app.onchain.atomic_arbitrage import as_atomic_opportunity, simulate_atomic_bundle
from app.onchain.chain_risk import ChainHealth, check_chain_health
from app.onchain.constants import DEFAULT_DEX_TRADE_SIZE_USD, DEX_STABLECOIN_BASE_ASSETS, DEX_VENUES, MIN_NET_EDGE_PCT
from app.onchain.cross_dex_arbitrage import detect_cross_dex_opportunity, order_buy_sell_pools
from app.onchain.dex_paper_trader import DEX_ATTEMPTABLE_STRATEGIES, DexCapitalPool, attempt_dex_trade
from app.onchain.flash_loan_research import FLASH_LOAN_EVM_CHAINS, build_flash_loan_opportunity, find_best_flash_loan_size
from app.onchain.gas_engine import RpcGasProvider
from app.onchain.market_data_provider import GeckoTerminalProvider
from app.onchain.models import DexPool
from app.onchain.multihop_arbitrage import build_token_graph, detect_multihop_opportunity
from app.onchain.pool_discovery import discover_pools
from app.onchain.ranking import apply_master_ranking_score
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

# Multi-Market Opportunity Engine, V5.5 (user directive, 2026-08-21) — the
# on-chain side of the Master Opportunity Engine. Its own OpportunityTracker
# instance (not the CEX one above) — full state isolation, not just a
# non-colliding key namespace, so a bug here can never corrupt CEX's
# in-memory tracking either. Polls on a much slower cadence than the
# event-driven CEX loop: GeckoTerminal's free (no API key) tier returns 429s
# under a burst of requests (confirmed live researching this feature), and
# a DEX price gap doesn't need sub-second detection the way a CEX order
# book does — the underlying pools only update once per block anyway.
DEX_POLL_INTERVAL_SECONDS = 45.0
# Bug found live, 2026-08-22: OpportunityTracker's default liveness
# (DEFAULT_LIVENESS_SECONDS=5.0) was calibrated for CEX's sub-second scan
# cadence — with DEX polling every ~45-53s, the SAME real, persisting
# mispricing was expiring and re-registering as "brand new" on every
# single cycle (confirmed live: a 3+ hour persisting BSC triangular
# mispricing produced 48 separate "new" opportunity rows, every one with
# updates_count=1, never a single continuation). This never caused a
# correctness bug in paper trading (every "new" row still got exactly one
# real, capital-tracked attempt — confirmed 100% 1:1 match in production
# data) but it did inflate "opportunities detected" well above the number
# of genuinely distinct real-world price gaps. 2x the poll interval gives
# real margin over normal cycle-to-cycle jitter without over-merging two
# genuinely different, back-to-back economic events.
dex_opportunity_tracker = OpportunityTracker(liveness_seconds=DEX_POLL_INTERVAL_SECONDS * 2)
_dex_market_data_provider = GeckoTerminalProvider()
_dex_gas_provider = RpcGasProvider()

# DEX Paper Trading (user directive, 2026-08-22) — a dedicated, isolated
# shadow capital pool, same size tier as CEX's own "5K" reference
# portfolio for comparability, entirely separate money/ledger (spec
# section 39). flash_loan_research never touches this (spec section 35).
DEX_PAPER_TRADING_CAPITAL_USD = 5_000.0
dex_capital_pool = DexCapitalPool(total_capital_usd=DEX_PAPER_TRADING_CAPITAL_USD)
_dex_paper_trading_rng = random.Random()

_NATIVE_TOKEN_SYMBOL_BY_CHAIN = {"eth": "WETH", "bsc": "WBNB", "solana": "SOL"}


def _find_native_token_price_usd(pools: list[DexPool], chain: str) -> float | None:
    """The gas engine needs the chain's native token priced in USD to
    convert a gas estimate (in ETH/BNB/SOL) into dollars — derived from
    whichever already-discovered pool happens to quote it against a
    stablecoin, rather than a second, redundant price fetch."""
    native = _NATIVE_TOKEN_SYMBOL_BY_CHAIN.get(chain)
    if native is None:
        return None
    for pool in pools:
        if pool.token0_symbol.upper() == native and pool.token1_symbol.upper() in ("USDC", "USDT"):
            return pool.price
        if pool.token1_symbol.upper() == native and pool.token0_symbol.upper() in ("USDC", "USDT"):
            return (1 / pool.price) if pool.price else None
    return None


async def dex_detection_loop() -> None:
    """Isolated on-chain detection loop (spec section 1: "CEX and ON-CHAIN
    engines must remain isolated. A bug in DEX must never stop or corrupt
    CEX.") — its own try/except boundary below is what makes that literally
    true: an exception here is caught, logged, and the loop keeps going on
    its own schedule, entirely independent of detection_loop() above."""
    while True:
        try:
            pools_by_venue: dict[tuple[str, str], list[DexPool]] = {}
            for venue in DEX_VENUES:
                pools = await discover_pools(_dex_market_data_provider, venue.chain, venue.dex)
                pools_by_venue[(venue.chain, venue.dex)] = pools
                logger.info("dex pool discovery: %s/%s — %d eligible pools", venue.chain, venue.dex, len(pools))

            scan_time = time.time()
            all_chains = sorted({venue.chain for venue in DEX_VENUES})
            async with async_session_factory() as session:
                for chain in all_chains:
                    venues_on_chain = [v for v in DEX_VENUES if v.chain == chain]

                    # Chain Risk (spec section 30) — "If chain is degraded:
                    # increase risk buffer or stop new opportunities."
                    # Chosen here: stop new opportunities outright on this
                    # chain for this cycle rather than trade against a gas
                    # assumption that's already stale, or against a
                    # network too congested to trust the RPC-fetched price.
                    chain_health = await check_chain_health(chain)
                    if chain_health in (ChainHealth.DEGRADED, ChainHealth.UNAVAILABLE):
                        logger.warning("dex detection: chain=%s health=%s — stopping new opportunities on it this cycle", chain, chain_health.value)
                        continue

                    chain_pools = [p for v in venues_on_chain for p in pools_by_venue[(v.chain, v.dex)]]
                    if not chain_pools:
                        continue

                    native_price_usd = _find_native_token_price_usd(chain_pools, chain)
                    if native_price_usd is None:
                        logger.warning(
                            "dex detection: no native token price available for chain=%s this cycle — skipping (no fabricated gas estimate)",
                            chain,
                        )
                        continue
                    gas_estimate = await _dex_gas_provider.estimate_gas_cost_usd(chain, native_price_usd)

                    new_opportunities: list = []

                    # Cross-DEX Arbitrage (spec section 5) — needs 2+
                    # distinct DEXs on this chain quoting the same pair.
                    if len(venues_on_chain) >= 2:
                        pools_by_pair: dict[tuple[str, str], list[DexPool]] = {}
                        for pool in chain_pools:
                            pair = tuple(sorted([pool.token0_symbol.upper(), pool.token1_symbol.upper()]))
                            pools_by_pair.setdefault(pair, []).append(pool)

                        for pools in pools_by_pair.values():
                            for i in range(len(pools)):
                                for j in range(i + 1, len(pools)):
                                    if pools[i].dex == pools[j].dex:
                                        continue  # same-venue "arbitrage" isn't real — needs 2 distinct DEXs
                                    opp = detect_cross_dex_opportunity(
                                        pools[i], pools[j], gas_estimate.gas_cost_usd, gas_estimate.gas_cost_usd
                                    )
                                    if opp is not None:
                                        new_opportunities.append(opp)

                                    # Flash Loan Research (spec sections
                                    # 9-11) — EVM-only (Aave v3's flash
                                    # loan is a real, documented EVM
                                    # protocol; Solana has no comparably
                                    # standardized equivalent, so this
                                    # isn't fabricated there). PAPER ONLY —
                                    # never borrows, never repays, never
                                    # touches a lending protocol. Reuses
                                    # the exact same pool ordering
                                    # detect_cross_dex_opportunity just
                                    # used, at flash-loan-sized amounts far
                                    # beyond what any own-capital portfolio
                                    # here could deploy.
                                    if chain in FLASH_LOAN_EVM_CHAINS:
                                        ordered = order_buy_sell_pools(pools[i], pools[j])
                                        if ordered is not None:
                                            buy_pool, sell_pool, buy_price, sell_price, theoretical_edge_pct = ordered
                                            best = find_best_flash_loan_size(
                                                buy_pool, sell_pool, buy_price, sell_price, gas_estimate.gas_cost_usd * 2
                                            )
                                            if best is not None:
                                                new_opportunities.append(
                                                    build_flash_loan_opportunity(buy_pool, sell_pool, best, theoretical_edge_pct, buy_price, sell_price)
                                                )

                    # Multi-Hop / DEX Triangular Arbitrage (spec sections 6,
                    # 7) — a token graph over EVERY pool on this chain,
                    # regardless of how many distinct DEXs contribute to
                    # it (unlike cross-DEX above, a cycle can walk through
                    # a single DEX's own pools, or mix DEXs — both are
                    # equally atomic within one on-chain transaction). One
                    # search per stablecoin base asset actually present in
                    # this chain's discovered pools.
                    graph = build_token_graph(chain_pools)
                    base_assets_present = {p.token0_symbol.upper() for p in chain_pools} | {p.token1_symbol.upper() for p in chain_pools}
                    for base_asset in base_assets_present & DEX_STABLECOIN_BASE_ASSETS:
                        opp = detect_multihop_opportunity(graph, base_asset, DEFAULT_DEX_TRADE_SIZE_USD, gas_estimate.gas_cost_usd, chain)
                        if opp is not None:
                            new_opportunities.append(opp)

                    # Atomic Arbitrage Research (spec section 8) — for every
                    # cross-DEX/multi-hop opportunity just found, also price
                    # what bundling it into one atomic transaction would be
                    # worth: no unhedged-leg risk, but gas is paid even on a
                    # revert and the naive "assume it always lands" profit
                    # is replaced by a probability-weighted expected value.
                    #
                    # REALITY AUDIT FIX (spec sections 2/9, user directive,
                    # 2026-08-22): atomic_opp and opp describe the SAME
                    # real-world price gap (identical legs/pools/detected_at
                    # — just two different execution METHODS for it), so
                    # both were previously appended and both independently
                    # attempted against the shared capital pool, double-
                    # booking one real economic event as two profitable
                    # trades. Confirmed live: 714 duplicate pairs, 179
                    # simultaneously "filled" for a combined $6,555.05 in
                    # duplicate-counted profit. Both are still persisted as
                    # detected opportunities (so the funnel keeps an honest
                    # raw-detection count and records which were duplicates
                    # of one underlying event), but only the
                    # higher-expected-value execution method for a given
                    # real event is ever attempted.
                    for opp in list(new_opportunities):
                        atomic_result = simulate_atomic_bundle(opp, gas_estimate.gas_cost_usd)
                        if atomic_result is None:
                            continue
                        atomic_opp = as_atomic_opportunity(opp, atomic_result)
                        if atomic_opp.net_spread_pct is not None and atomic_opp.net_spread_pct >= MIN_NET_EDGE_PCT:
                            new_opportunities.append(atomic_opp)
                            opp_ev = opp.expected_profit_usd or 0.0
                            atomic_ev = atomic_opp.expected_profit_usd or 0.0
                            loser = atomic_opp if atomic_ev <= opp_ev else opp
                            loser.rejection_reason = "duplicate_economic_event"

                    # Master Opportunity Ranker (spec sections 17-18) —
                    # every DEX opportunity, regardless of which of the 4
                    # strategies above found it, gets scored on the exact
                    # same capital_velocity_score/return_per_minute_pct
                    # scale a CEX opportunity already carries — the same
                    # "Net Profit / Capital-Minute, not just absolute
                    # profit" comparison, generalized rather than rebuilt.
                    for opp in new_opportunities:
                        apply_master_ranking_score(opp)

                    for opp in new_opportunities:
                        observation = dex_opportunity_tracker.observe(opp, now=scan_time)
                        if observation.is_new:
                            await save_opportunity(session, opp)
                        else:
                            # PRE-PHASE-2 CORRECTIVE MAINTENANCE (2026-08-22):
                            # was hardcoded rejection_reason=None, which
                            # ERASED the duplicate_economic_event marking
                            # (set on `opp` earlier this same cycle by the
                            # atomic-dedup loop above) on every single
                            # continuation update — found live during the
                            # Reality Audit's Mission 3 verification (the
                            # persisted duplicate count was ~50% of the true,
                            # directly-verified count). Passing opp's own
                            # freshly-recomputed rejection_reason instead
                            # exactly matches the CEX continuation call site's
                            # already-correct pattern just below (line ~446)
                            # — the dedup decision is re-evaluated fresh
                            # every cycle and persisted as-is, never silently
                            # overwritten with a stale None.
                            await update_opportunity_tracking(session, observation.tracked, opp, rejection_reason=opp.rejection_reason)
                            continue  # a continuation of an already-attempted signal — never re-attempt it every cycle

                        # DEX Paper Trading (user directive, 2026-08-22) —
                        # every GENUINELY NEW executable opportunity gets a
                        # real attempt against the shared DEX capital pool.
                        # flash_loan_research is deliberately excluded
                        # (spec section 35: never reduce available own
                        # capital — that pool represents real own capital
                        # shadow trading, borrowed amounts are a separate
                        # research question). duplicate_economic_event is
                        # also excluded (reality audit fix, above) — it's
                        # the lower-EV twin of an opportunity already being
                        # attempted this same cycle under a different
                        # execution method, not a second real opportunity.
                        if opp.strategy in DEX_ATTEMPTABLE_STRATEGIES and opp.rejection_reason != "duplicate_economic_event":
                            attempt = attempt_dex_trade(opp, dex_capital_pool, gas_estimate.gas_cost_usd, _dex_paper_trading_rng, now=scan_time)
                            await save_dex_trade_attempt(session, attempt)

                for tracked in dex_opportunity_tracker.expire_stale(now=scan_time):
                    await close_opportunity_tracking(session, tracked, closed_at=scan_time)
                await session.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("dex detection loop iteration failed — CEX detection loop is unaffected")
        await asyncio.sleep(DEX_POLL_INTERVAL_SECONDS)


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
            scan_id = str(uuid.uuid4())  # PHASE 2B (user directive, 2026-08-22) — groups this cycle's telemetry rows, see below
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

                    # PHASE 2B — CEX SCAN-LEVEL SHADOW TELEMETRY (user
                    # directive, 2026-08-22). Read-only observability tap:
                    # records exactly what OLD decided THIS scan cycle —
                    # continuations included, not just "new" detections —
                    # closing the gap the Phase 2 final validation found
                    # (MASTER only ever saw "new" opportunities rows, while
                    # OLD re-validates, and can re-approve, a persisting
                    # opportunity every single cycle, continuously
                    # refreshing position_tracker's lock in a way that's
                    # invisible to any evaluation based on the
                    # opportunities table alone). position_already_open is
                    # independently re-derived here (position_tracker.is_open
                    # is a pure read, mutates nothing) using the SAME key
                    # app.execution.validator.validate() itself just used,
                    # so the telemetry row carries this fact even when
                    # validate() short-circuited on an earlier gate before
                    # ever reaching its own position check. Wrapped in its
                    # own try/except — a telemetry failure must NEVER
                    # interrupt OLD's real trade processing for this or any
                    # other opportunity this cycle; it changes nothing
                    # about what OLD decides or does, purely additive.
                    try:
                        position_already_open = False
                        if opp.holding_period_seconds is not None and opp.legs:
                            scan_position_key = (opp.strategy, opp.legs[0].get("exchange"), opp.symbol)
                            position_already_open = position_tracker.is_open(scan_position_key, scan_time)
                        await save_cex_scan_event(
                            session,
                            scan_id=scan_id,
                            scanned_at=datetime.fromtimestamp(scan_time, tz=UTC).replace(tzinfo=None),
                            opportunity_id=opp.id,
                            is_new_detection=observation.is_new,
                            strategy=opp.strategy,
                            symbol=opp.symbol,
                            legs=opp.legs,
                            expected_profit_usd=opp.expected_profit_usd,
                            capital_usd=opp.capital_usd,
                            net_spread_pct=opp.net_spread_pct,
                            execution_fill_probability=opp.execution_fill_probability,
                            holding_period_seconds=opp.holding_period_seconds,
                            capital_velocity_score=opp.capital_velocity_score,
                            position_already_open=position_already_open,
                            old_approved=validation.approved,
                            old_rejection_reason=opp.rejection_reason,
                        )
                    except Exception:
                        logger.exception("CEX scan telemetry write failed (Phase 2B) — OLD engine unaffected, continuing")

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

    # Multi-Market Opportunity Engine, V5.5 (user directive, 2026-08-21) —
    # a fully separate background task from detection_loop above: its own
    # try/except, its own tracker instance, its own poll cadence. Shadow
    # mode only (spec section 23) — detected/persisted/tracked exactly like
    # CEX opportunities, but never handed to paper_trader or any
    # VirtualPortfolio, so it can never affect CEX capital or historical
    # P&L (spec section 39).
    background_tasks.append(asyncio.create_task(dex_detection_loop(), name="dex_detection_loop"))

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
