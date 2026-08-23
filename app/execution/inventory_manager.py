"""AUTOMATIC CROSS-EXCHANGE INVENTORY MANAGER (user directive,
2026-08-23) — SIMULATION / READ-ONLY ONLY.

Closes the exact gap the multi-symbol preflight kept surfacing once both
real pools were funded (Binance ~102 USDT, Bybit ~64 USDT,
2026-08-23): funding both exchanges with USDT only unlocks the BUY side
of an arbitrage. The SELL side of app.execution.live_ranker's own
PrePositioningCheck still requires the BASE ASSET itself to already be
held on the sell exchange — check_prepositioning() there has always
required this, it's just that nothing has ever held any base-asset
inventory, so every candidate showed PREPOSITIONED=NO no matter how
much USDT sat on either exchange (confirmed empirically:
qualified_opportunities=0 against a live 239-symbol/120-direction scan
with both pools funded).

This module answers three questions, all read-only:

1. Per currently-ranked opportunity: is the required BASE_ASSET already
   held on the SELL_EXCHANGE right now? (OpportunityInventoryCheck,
   reusing app.execution.live_ranker's own balance check rather than
   re-implementing it — one source of truth for "prepositioned".)
2. Which assets have a real, recurring, net-positive track record that
   would justify holding a small standing inventory of them?
   (InventoryScoreBreakdown, built from app.reporting.altcoin_scan_report's
   already-persisted, time-spanning observation history — NEVER from a
   single live quote, so a one-off vanishing spread can never itself
   trigger a buy recommendation. This is explicitly scoped to whatever
   altcoin_scanner.py's own watchlist covers; the full ~240-symbol
   dynamic universe has no persisted history yet, and this module does
   not pretend otherwise — see inventory_scores' honest observations
   count.)
3. Given (1) and (2) plus the hard limits below, what rebalancing WOULD
   MASTER recommend? (RebalanceRecommendation — every single one carries
   simulated=True and this module contains no code path that submits an
   order. Wiring a recommendation to a real Binance/Bybit spot trade is
   a distinct, separately-authorized future step: "Ne laisse pas
   l'Inventory Manager convertir réellement les 60 USDT Bybit tant que
   son comportement et ses limites n'ont pas été vérifiés.")

Hard limits (app.config.settings): MAX_INVENTORY_PER_ASSET_USDT,
MAX_TOTAL_INVENTORY_EXPOSURE_USDT, MAX_REBALANCE_SIZE_USDT,
MIN_EXPECTED_REUSE_COUNT — enforced inside recommend_rebalance() so the
160 USDT pool can never fragment across too many altcoins even as pure
recommendations.

Rebalancing is Spot-only by construction: this module only ever reads
balances via BinanceAccountClient/BybitClient (both Spot-only clients —
neither exposes a futures/margin/borrow method anywhere in this
codebase) and produces data, never an order. See
tests/test_inventory_manager_isolation.py for the mechanical proof.
"""

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.execution.binance_account_client import BinanceAccountClient
from app.execution.bybit_client import BybitClient, parse_all_wallet_balances, parse_wallet_balance
from app.execution.live_ranker import RankedOpportunity, rank_live_opportunities
from app.reporting.altcoin_scan_report import (
    MIN_OBSERVATIONS_TO_JUDGE,
    STRONG_MIN_NET_PROFIT_PER_1000USDT,
    STRONG_MIN_PERSISTENCE_SECONDS,
    DirectionSummary,
    OpportunityStatus,
    build_altcoin_scan_report,
)
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher

logger = logging.getLogger(__name__)

QUOTE_ASSET = "USDT"
DEFAULT_LOOKBACK = timedelta(hours=24)
DEFAULT_MAX_RANKER_SYMBOLS = 30  # same latency-bound reasoning as live_preflight's max_symbols=60 — kept smaller since this report also does a valuation pass


@dataclass(slots=True)
class OpportunityInventoryCheck:
    """Item 1 of the directive: BUY_EXCHANGE, SELL_EXCHANGE,
    REQUIRED_QUOTE_ASSET, REQUIRED_BASE_ASSET, CURRENT_INVENTORY,
    INVENTORY_READY — derived 1:1 from live_ranker's own
    PrePositioningCheck, never recomputed independently."""

    symbol: str
    buy_exchange: str
    sell_exchange: str
    required_quote_asset: str
    required_quote_amount: float
    required_base_asset: str
    required_base_amount: float
    current_base_inventory: float
    inventory_ready: bool
    status: str  # "EXECUTABLE_NOW" | "NOT_EXECUTABLE_NOW"
    reason: str | None  # None when executable now, else "INVENTORY_MISSING" | "INSUFFICIENT_BUY_CAPITAL"


@dataclass(slots=True)
class InventoryScoreBreakdown:
    """INVENTORY_SCORE (item 3 of the directive) — frequency, net edge,
    persistence, liquidity, volatility and expected reuse, each a real
    figure from app.reporting.altcoin_scan_report's persisted history,
    never a single scan tick."""

    symbol: str
    base_asset: str
    observations: int
    frequency_score: float
    net_edge_score: float
    persistence_score: float
    liquidity_score: float
    volatility_score: float
    expected_reuse_score: float
    total_score: float  # 0-100
    expected_reuse_count: int
    eligible_for_prepositioning: bool
    reason: str


@dataclass(slots=True)
class RebalanceRecommendation:
    action: str  # "BUY_INVENTORY" | "SELL_INVENTORY"
    exchange: str
    asset: str
    recommended_notional_usdt: float
    current_holding_usdt_equiv: float
    inventory_score: float | None
    reason: str
    simulated: bool  # ALWAYS True in this phase — see module docstring


@dataclass(slots=True)
class ExchangeInventorySnapshot:
    exchange: str
    usdt_available: float
    holdings: dict[str, float] = field(default_factory=dict)  # non-USDT, non-zero only
    holdings_usdt_value: dict[str, float] = field(default_factory=dict)  # mark-to-market, live mid price


@dataclass(slots=True)
class InventoryManagerReport:
    generated_at: datetime
    binance: ExchangeInventorySnapshot
    bybit: ExchangeInventorySnapshot
    total_usdt_available: float
    capital_locked_in_inventory_usdt: float
    prepositioned_assets: list[str]
    inventory_missing: list[OpportunityInventoryCheck]
    inventory_scores: list[InventoryScoreBreakdown]
    rebalance_candidates: list[RebalanceRecommendation]
    inventory_pnl_usd: float | None  # None until this module (or its authorized successor) has actually opened a tracked position
    inventory_pnl_note: str
    simulation_only: bool  # ALWAYS True in this phase


def _to_opportunity_check(r: RankedOpportunity) -> OpportunityInventoryCheck:
    p = r.prepositioning
    inventory_ready = p.available_sell_balance >= p.required_sell_qty
    buy_ready = p.available_buy_balance_usdt >= p.required_buy_balance_usdt
    if inventory_ready and buy_ready:
        status, reason = "EXECUTABLE_NOW", None
    elif not inventory_ready:
        status, reason = "NOT_EXECUTABLE_NOW", "INVENTORY_MISSING"
    else:
        status, reason = "NOT_EXECUTABLE_NOW", "INSUFFICIENT_BUY_CAPITAL"
    return OpportunityInventoryCheck(
        symbol=r.symbol,
        buy_exchange=r.buy_exchange,
        sell_exchange=r.sell_exchange,
        required_quote_asset=QUOTE_ASSET,
        required_quote_amount=p.required_buy_balance_usdt,
        required_base_asset=p.required_sell_asset,
        required_base_amount=p.required_sell_qty,
        current_base_inventory=p.available_sell_balance,
        inventory_ready=inventory_ready,
        status=status,
        reason=reason,
    )


def score_direction_for_inventory(summary: DirectionSummary, min_expected_reuse_count: int) -> InventoryScoreBreakdown:
    """Same normalization convention as altcoin_scan_report.market_priority_score
    (each sub-score capped to [0, 1] against the same STRONG_* reference
    thresholds already validated in that module) but combined as a
    weighted sum, not a product — a product of six sub-1.0 terms
    collapses almost everything toward zero and stops being a usable
    ranking signal; a weighted sum stays interpretable as a 0-100 score
    while still being driven by the same real inputs."""
    base_asset = summary.symbol.split("/")[0]
    frequency_score = min(1.0, summary.unique_detections / 10.0)
    net_edge_score = min(1.0, max(0.0, summary.net_profit_per_1000usdt_mean / STRONG_MIN_NET_PROFIT_PER_1000USDT))
    persistence_score = min(1.0, summary.mean_persistence_seconds / STRONG_MIN_PERSISTENCE_SECONDS) if summary.mean_persistence_seconds > 0 else 0.0
    liquidity_score = 1.0 if summary.observations >= MIN_OBSERVATIONS_TO_JUDGE else summary.observations / MIN_OBSERVATIONS_TO_JUDGE
    volatility_score = min(1.0, max(0.0, summary.gross_spread_max_pct - summary.gross_spread_mean_pct) / 1.0)
    expected_reuse_count = summary.unique_detections + summary.continuations
    expected_reuse_score = min(1.0, expected_reuse_count / (min_expected_reuse_count * 3))

    total_score = 100.0 * (
        0.30 * net_edge_score
        + 0.20 * frequency_score
        + 0.15 * persistence_score
        + 0.15 * liquidity_score
        + 0.10 * volatility_score
        + 0.10 * expected_reuse_score
    )

    # Never pre-position for a one-off, already-disappearing spread: a
    # symbol needs MIN_EXPECTED_REUSE_COUNT independent sightings
    # (new-detection + continuation rows, spanning real elapsed time)
    # before it's even eligible, on top of the usual STRONG/WATCH bar.
    if summary.observations < MIN_OBSERVATIONS_TO_JUDGE:
        eligible = False
        reason = f"insufficient history ({summary.observations} observation(s), need >= {MIN_OBSERVATIONS_TO_JUDGE})"
    elif expected_reuse_count < min_expected_reuse_count:
        eligible = False
        reason = f"seen only {expected_reuse_count} time(s) — need >= {min_expected_reuse_count} independent sightings, never pre-position for a one-off spread"
    elif summary.net_profit_per_1000usdt_mean <= 0:
        eligible = False
        reason = "no net-positive edge after real fees"
    elif summary.status not in (OpportunityStatus.STRONG, OpportunityStatus.WATCH):
        eligible = False
        reason = f"status too weak ({summary.status.value})"
    else:
        eligible = True
        reason = f"recurring net-positive edge, {expected_reuse_count} sightings, status {summary.status.value}"

    return InventoryScoreBreakdown(
        symbol=summary.symbol,
        base_asset=base_asset,
        observations=summary.observations,
        frequency_score=frequency_score,
        net_edge_score=net_edge_score,
        persistence_score=persistence_score,
        liquidity_score=liquidity_score,
        volatility_score=volatility_score,
        expected_reuse_score=expected_reuse_score,
        total_score=total_score,
        expected_reuse_count=expected_reuse_count,
        eligible_for_prepositioning=eligible,
        reason=reason,
    )


def recommend_rebalance(
    scores: list[InventoryScoreBreakdown],
    missing: list[OpportunityInventoryCheck],
    current_inventory_usdt_value: dict[str, float],
    binance_usdt: float,
    bybit_usdt: float,
    max_inventory_per_asset_usdt: float,
    max_total_inventory_exposure_usdt: float,
    max_rebalance_size_usdt: float,
) -> list[RebalanceRecommendation]:
    """Pure function, no I/O — every recommendation carries simulated=True.
    Two kinds of recommendation:

    SELL_INVENTORY: an asset currently held whose real track record no
    longer justifies holding it (never qualified, or qualified once but
    the signal has gone stale) — MASTER's own "reconvert unused
    inventory" capability (directive item 5), still simulation-only here.

    BUY_INVENTORY: a currently-missing (item 1), historically-proven
    (item 3) asset, sized under all four hard limits at once so the
    capital pool can never fragment across too many altcoins."""
    recommendations: list[RebalanceRecommendation] = []
    score_by_asset = {s.base_asset: s for s in scores}

    for asset, usdt_value in current_inventory_usdt_value.items():
        if usdt_value <= 0:
            continue
        s = score_by_asset.get(asset)
        if s is not None and s.eligible_for_prepositioning:
            continue
        reason = "no recent qualifying opportunity history for this asset" if s is None else s.reason
        recommendations.append(
            RebalanceRecommendation(
                action="SELL_INVENTORY",
                exchange="binance_or_bybit",  # both legs' exact holding is in ExchangeInventorySnapshot; this recommendation covers the asset overall
                asset=asset,
                recommended_notional_usdt=round(usdt_value, 2),
                current_holding_usdt_equiv=round(usdt_value, 2),
                inventory_score=s.total_score if s is not None else None,
                reason=f"reconvert to USDT — {reason}",
                simulated=True,
            )
        )

    total_locked = sum(v for v in current_inventory_usdt_value.values() if v > 0)
    remaining_exposure_capacity = max(0.0, max_total_inventory_exposure_usdt - total_locked)
    missing_by_asset = {c.required_base_asset: c for c in missing if c.reason == "INVENTORY_MISSING"}
    remaining_usdt_by_exchange = {"binance": binance_usdt, "bybit": bybit_usdt}

    eligible_sorted = sorted(
        (s for s in scores if s.eligible_for_prepositioning and s.base_asset in missing_by_asset),
        key=lambda s: s.total_score,
        reverse=True,
    )
    for s in eligible_sorted:
        if remaining_exposure_capacity <= 0:
            break
        already_held = current_inventory_usdt_value.get(s.base_asset, 0.0)
        headroom = max_inventory_per_asset_usdt - already_held
        if headroom <= 0:
            continue
        matching = missing_by_asset[s.base_asset]
        exchange = matching.sell_exchange
        available = remaining_usdt_by_exchange.get(exchange, 0.0)
        size = min(max_rebalance_size_usdt, headroom, remaining_exposure_capacity, available)
        if size <= 0:
            continue
        recommendations.append(
            RebalanceRecommendation(
                action="BUY_INVENTORY",
                exchange=exchange,
                asset=s.base_asset,
                recommended_notional_usdt=round(size, 2),
                current_holding_usdt_equiv=round(already_held, 2),
                inventory_score=s.total_score,
                reason=f"recurring net-positive opportunity ({s.expected_reuse_count}x seen, status feeds score {s.total_score:.0f}/100), currently blocking execution on {exchange} — INVENTORY_MISSING",
                simulated=True,
            )
        )
        remaining_exposure_capacity -= size
        remaining_usdt_by_exchange[exchange] -= size

    return recommendations


async def _value_holdings(fetcher: MultiExchangeSnapshotFetcher, exchange: str, holdings: dict[str, float]) -> dict[str, float]:
    values: dict[str, float] = {}
    for asset, qty in holdings.items():
        if qty <= 0:
            continue
        try:
            data = await fetcher.fetch(exchange, f"{asset}/{QUOTE_ASSET}")
        except Exception as exc:
            logger.warning("inventory-manager: valuation fetch failed for %s/%s on %s: %s", asset, QUOTE_ASSET, exchange, exc)
            data = None
        if data is None:
            values[asset] = 0.0  # unknown price is reported as 0, never fabricated
            continue
        mid = (data.best_bid + data.best_ask) / 2.0
        values[asset] = qty * mid
    return values


async def build_inventory_report(
    session: AsyncSession,
    binance_read: BinanceAccountClient | None = None,
    bybit_read: BybitClient | None = None,
    max_ranker_symbols: int = DEFAULT_MAX_RANKER_SYMBOLS,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> InventoryManagerReport:
    settings = get_settings()
    binance_read = binance_read or BinanceAccountClient()
    bybit_read = bybit_read or BybitClient()

    binance_snapshot = await binance_read.get_account_snapshot()
    binance_usdt = binance_snapshot.balance_usdt() if binance_snapshot is not None else 0.0
    binance_holdings = {b.asset: b.free for b in (binance_snapshot.balances if binance_snapshot is not None else []) if b.asset != QUOTE_ASSET and b.free > 0}

    wallet = await bybit_read.get_wallet_balance()
    bybit_usdt = parse_wallet_balance(wallet, QUOTE_ASSET)
    bybit_holdings = {a: q for a, q in parse_all_wallet_balances(wallet).items() if a != QUOTE_ASSET and q > 0}

    ranked = await rank_live_opportunities(binance_read=binance_read, bybit_read=bybit_read, max_symbols=max_ranker_symbols)
    opportunity_checks = [_to_opportunity_check(r) for r in ranked]
    inventory_missing = [c for c in opportunity_checks if c.reason == "INVENTORY_MISSING"]
    prepositioned_assets = sorted({c.required_base_asset for c in opportunity_checks if c.inventory_ready})

    since = datetime.now(UTC) - lookback
    scan_report = await build_altcoin_scan_report(session, since=since)
    inventory_scores = [score_direction_for_inventory(s, settings.min_expected_reuse_count) for s in scan_report.best_direction_by_symbol]
    inventory_scores.sort(key=lambda s: s.total_score, reverse=True)

    fetcher = MultiExchangeSnapshotFetcher(binance=binance_read, bybit=bybit_read)
    binance_value = await _value_holdings(fetcher, "binance", binance_holdings)
    bybit_value = await _value_holdings(fetcher, "bybit", bybit_holdings)

    combined_value: dict[str, float] = dict(binance_value)
    for asset, value in bybit_value.items():
        combined_value[asset] = combined_value.get(asset, 0.0) + value
    capital_locked = sum(combined_value.values())

    recommendations = recommend_rebalance(
        scores=inventory_scores,
        missing=inventory_missing,
        current_inventory_usdt_value=combined_value,
        binance_usdt=binance_usdt,
        bybit_usdt=bybit_usdt,
        max_inventory_per_asset_usdt=settings.max_inventory_per_asset_usdt,
        max_total_inventory_exposure_usdt=settings.max_total_inventory_exposure_usdt,
        max_rebalance_size_usdt=settings.max_rebalance_size_usdt,
    )

    return InventoryManagerReport(
        generated_at=datetime.now(UTC),
        binance=ExchangeInventorySnapshot(exchange="binance", usdt_available=binance_usdt, holdings=binance_holdings, holdings_usdt_value=binance_value),
        bybit=ExchangeInventorySnapshot(exchange="bybit", usdt_available=bybit_usdt, holdings=bybit_holdings, holdings_usdt_value=bybit_value),
        total_usdt_available=binance_usdt + bybit_usdt,
        capital_locked_in_inventory_usdt=capital_locked,
        prepositioned_assets=prepositioned_assets,
        inventory_missing=inventory_missing,
        inventory_scores=inventory_scores,
        rebalance_candidates=recommendations,
        inventory_pnl_usd=None,
        inventory_pnl_note="not yet trackable — no inventory position has ever been opened by this system (simulation-only; recommend_rebalance's output is never executed)",
        simulation_only=True,
    )
