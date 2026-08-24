"""AUTOMATIC CROSS-EXCHANGE INVENTORY MANAGER — V2, FULL DYNAMIC UNIVERSE
(user directive, 2026-08-23, extended 2026-08-24) — SIMULATION /
READ-ONLY ONLY.

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
   trigger a buy recommendation. As of V2, altcoin_scanner.py covers the
   FULL dynamic Binance∩Bybit universe via its own two-stage scan
   [app.scanner.fast_discovery], not a fixed watchlist — coverage on any
   given symbol still depends on it having cleared STAGE A recently
   enough to accumulate STAGE B history, which is why `observations`
   stays an honestly-reported number rather than assumed complete.)
3. Given (1) and (2) plus the hard limits below, what rebalancing WOULD
   MASTER recommend, classified OBSERVE / PREPOSITION_CANDIDATE /
   STRONG_PREPOSITION_CANDIDATE / DO_NOT_PREPOSITION? (RebalanceRecommendation
   — every single one carries simulated=True and this module contains no
   code path that submits an order. Wiring a recommendation to a real
   Binance/Bybit spot trade is a distinct, separately-authorized future
   step: "Ne laisse pas l'Inventory Manager convertir réellement les 60
   USDT Bybit tant que son comportement et ses limites n'ont pas été
   vérifiés." INVENTORY_MANAGER_MODE/AUTO_REAL_REBALANCE in
   app.config.settings exist only to be surfaced on every report — this
   codebase never flips them itself.)

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
from enum import StrEnum

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
    build_altcoin_scan_report,
)
from app.reporting.short_term_regime import ShortTermRegime, ShortTermRegimeSummary, build_short_term_regimes
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher

logger = logging.getLogger(__name__)

QUOTE_ASSET = "USDT"
DEFAULT_LOOKBACK = timedelta(hours=24)
ONE_HOUR_LOOKBACK = timedelta(hours=1)  # informational-only comparison window (item 6/12) — never a gate, see score_direction_for_inventory
DEFAULT_MAX_RANKER_SYMBOLS = 30  # same latency-bound reasoning as live_preflight's max_symbols=60 — kept smaller since this report also does a valuation pass


class InventoryClassification(StrEnum):
    OBSERVE = "OBSERVE"
    PREPOSITION_CANDIDATE = "PREPOSITION_CANDIDATE"
    STRONG_PREPOSITION_CANDIDATE = "STRONG_PREPOSITION_CANDIDATE"
    DO_NOT_PREPOSITION = "DO_NOT_PREPOSITION"


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
    """INVENTORY_SCORE (item 3/4 of the directive) — frequency, net edge,
    persistence, liquidity, volatility and expected reuse, each a real
    figure from app.reporting.altcoin_scan_report's persisted history,
    never a single scan tick. median/p10 edge (not just the mean) and
    net_positive_rate_pct are what let classification tell a
    consistently-profitable symbol apart from one whose average is
    positive only because of one lucky tick."""

    symbol: str
    base_asset: str
    observations: int
    sightings: int  # independent detections + continuations — "how many separate times has this repeated"
    net_positive_rate_pct: float
    median_net_edge_per_1000usdt: float
    p10_net_edge_per_1000usdt: float  # worst-case-but-not-outlier edge
    frequency_score: float
    net_edge_score: float
    persistence_score: float
    liquidity_score: float
    volatility_score: float
    expected_reuse_score: float
    total_score: float  # 0-100
    expected_reuse_label: str  # "LOW" | "MEDIUM" | "HIGH" — a stated heuristic bucket, never a fabricated precise forecast
    # V2.1 (user directive, 2026-08-24, item 7) — "if this asset were
    # pre-positioned starting now, roughly how many of the trades we
    # already observed being blocked would additionally have executed":
    # the simplest honest projection is that the SAME historical rate
    # (sightings) continues, since nothing more sophisticated is
    # justified by one lookback window's worth of data. Never a fitted
    # statistical forecast — just the plain count restated as a
    # forward-looking estimate.
    expected_additional_executable_trades: int
    classification: InventoryClassification
    reason: str
    # FINAL SIMPLIFICATION (user directive, 2026-08-24) — classification
    # is now DRIVEN by the short-term regime (app.reporting.
    # short_term_regime), not by the 1h/24h mean. These fields surface
    # exactly what drove the decision; the 1h/24h means are kept purely
    # informational (item 2: "conserve les données ... pas comme hard
    # gate").
    short_term_regime: str = "NO_DATA"
    edge_now_net_profit_per_1000usdt: float | None = None
    confirmations_recent: int = 0
    current_streak_seconds: float = 0.0
    mean_net_profit_1h_usdt: float | None = None
    mean_net_profit_24h_usdt: float | None = None


@dataclass(slots=True)
class RebalanceRecommendation:
    action: str  # "BUY_INVENTORY" | "SELL_INVENTORY"
    exchange: str
    asset: str
    recommended_notional_usdt: float
    current_holding_usdt_equiv: float
    capital_required_usdt: float
    inventory_score: float | None
    classification: str | None
    sightings: int | None
    net_positive_rate_pct: float | None
    median_net_edge: float | None
    p10_net_edge: float | None
    expected_reuse_label: str | None
    expected_additional_executable_trades: int | None
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


def _expected_reuse_label(sightings: int, min_expected_reuse_count: int) -> str:
    """A stated heuristic bucket, not a predictive forecast — this
    module has no genuine model of FUTURE reuse probability, only a
    HISTORICAL sightings count, and labeling it as a vague bucket rather
    than inventing a fake precise number is the honest choice."""
    if sightings < min_expected_reuse_count * 2:
        return "LOW"
    if sightings < min_expected_reuse_count * 5:
        return "MEDIUM"
    return "HIGH"


def is_preposition_eligible(s: InventoryScoreBreakdown) -> bool:
    return s.classification in (InventoryClassification.PREPOSITION_CANDIDATE, InventoryClassification.STRONG_PREPOSITION_CANDIDATE)


def score_direction_for_inventory(
    summary: DirectionSummary,
    min_expected_reuse_count: int,
    short_term: ShortTermRegimeSummary | None = None,
) -> InventoryScoreBreakdown:
    """FINAL SIMPLIFICATION (user directive, 2026-08-24) — this is
    short-term cross-exchange arbitrage: an opportunity can be excellent
    for seconds or a few minutes, and classification must reflect the
    CURRENT regime, not a 1h/24h average that was never the right time
    horizon for this strategy (that mismatch is exactly what wrongly
    blocked a genuinely good, currently-confirmed RVN opportunity — see
    app.reporting.short_term_regime's own docstring).

    Classification is now DRIVEN by short_term.regime (a strictly
    harder-to-reach ladder: NO_EDGE < FLASH < CONFIRMED_SHORT_TERM <
    PERSISTENT/STRONG_PERSISTENT — see classify_short_term_regime):
      DO_NOT_PREPOSITION — no positive edge right now (or no recent data
        at all to judge from).
      OBSERVE — positive right now (FLASH) but not yet independently
        confirmed enough times to rule out a one-off spread.
      PREPOSITION_CANDIDATE — CONFIRMED_SHORT_TERM: enough independent
        recent confirmations AND positive right now. Already tradable —
        never made to wait for multi-minute persistence.
      STRONG_PREPOSITION_CANDIDATE — PERSISTENT or STRONG_PERSISTENT: the
        edge has held continuously for 5+ / 15+ minutes. Higher
        confidence, can justify more inventory later, but is NOT a
        prerequisite for the first small trade.

    summary (the 24h-window DirectionSummary) now only feeds total_score
    — a continuous ranking signal used to prioritize AMONG regime-eligible
    candidates in recommend_rebalance — and is NEVER able to force
    DO_NOT_PREPOSITION on its own (item 2: "conserve les données 1h/24h
    pour analytics/risk scoring, mais pas comme hard gate"). Same
    normalization convention as altcoin_scan_report.market_priority_score
    (each sub-score capped to [0, 1] against the same STRONG_* reference
    thresholds already validated in that module) but combined as a
    weighted sum, not a product — a product of six sub-1.0 terms
    collapses almost everything toward zero and stops being a usable
    ranking signal."""
    base_asset = summary.symbol.split("/")[0]
    frequency_score = min(1.0, summary.unique_detections / 10.0)
    net_edge_score = min(1.0, max(0.0, summary.net_profit_per_1000usdt_mean / STRONG_MIN_NET_PROFIT_PER_1000USDT))
    persistence_score = min(1.0, summary.mean_persistence_seconds / STRONG_MIN_PERSISTENCE_SECONDS) if summary.mean_persistence_seconds > 0 else 0.0
    liquidity_score = 1.0 if summary.observations >= MIN_OBSERVATIONS_TO_JUDGE else summary.observations / MIN_OBSERVATIONS_TO_JUDGE
    volatility_score = min(1.0, max(0.0, summary.gross_spread_max_pct - summary.gross_spread_mean_pct) / 1.0)
    sightings = summary.unique_detections + summary.continuations
    expected_reuse_score = min(1.0, sightings / (min_expected_reuse_count * 3))

    total_score = 100.0 * (
        0.30 * net_edge_score
        + 0.20 * frequency_score
        + 0.15 * persistence_score
        + 0.15 * liquidity_score
        + 0.10 * volatility_score
        + 0.10 * expected_reuse_score
    )

    if short_term is None:
        classification = InventoryClassification.DO_NOT_PREPOSITION
        reason = "no recent short-term observation available — cannot judge the current regime"
    elif short_term.regime == ShortTermRegime.NO_EDGE:
        classification = InventoryClassification.DO_NOT_PREPOSITION
        reason = short_term.regime_reason
    elif short_term.regime == ShortTermRegime.FLASH:
        classification = InventoryClassification.OBSERVE
        reason = short_term.regime_reason
    elif short_term.regime == ShortTermRegime.CONFIRMED_SHORT_TERM:
        classification = InventoryClassification.PREPOSITION_CANDIDATE
        reason = short_term.regime_reason
    else:  # PERSISTENT or STRONG_PERSISTENT
        classification = InventoryClassification.STRONG_PREPOSITION_CANDIDATE
        reason = short_term.regime_reason

    return InventoryScoreBreakdown(
        symbol=summary.symbol,
        base_asset=base_asset,
        observations=summary.observations,
        sightings=sightings,
        net_positive_rate_pct=summary.positive_rate_pct,
        median_net_edge_per_1000usdt=summary.net_profit_per_1000usdt_median,
        p10_net_edge_per_1000usdt=summary.net_profit_per_1000usdt_p10,
        frequency_score=frequency_score,
        net_edge_score=net_edge_score,
        persistence_score=persistence_score,
        liquidity_score=liquidity_score,
        volatility_score=volatility_score,
        expected_reuse_score=expected_reuse_score,
        total_score=total_score,
        expected_reuse_label=_expected_reuse_label(sightings, min_expected_reuse_count),
        expected_additional_executable_trades=sightings,
        classification=classification,
        reason=reason,
        short_term_regime=short_term.regime.value if short_term is not None else "NO_DATA",
        edge_now_net_profit_per_1000usdt=short_term.edge_now_net_profit_per_1000usdt if short_term is not None else None,
        confirmations_recent=short_term.confirmations_recent if short_term is not None else 0,
        current_streak_seconds=short_term.current_streak_seconds if short_term is not None else 0.0,
        mean_net_profit_1h_usdt=short_term.mean_net_profit_1h_usdt if short_term is not None else None,
        mean_net_profit_24h_usdt=summary.net_profit_per_1000usdt_mean,
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
        if s is not None and is_preposition_eligible(s):
            continue
        reason = "no recent qualifying opportunity history for this asset" if s is None else s.reason
        recommendations.append(
            RebalanceRecommendation(
                action="SELL_INVENTORY",
                exchange="binance_or_bybit",  # both legs' exact holding is in ExchangeInventorySnapshot; this recommendation covers the asset overall
                asset=asset,
                recommended_notional_usdt=round(usdt_value, 2),
                current_holding_usdt_equiv=round(usdt_value, 2),
                capital_required_usdt=round(usdt_value, 2),
                inventory_score=s.total_score if s is not None else None,
                classification=s.classification.value if s is not None else None,
                sightings=s.sightings if s is not None else None,
                net_positive_rate_pct=s.net_positive_rate_pct if s is not None else None,
                median_net_edge=s.median_net_edge_per_1000usdt if s is not None else None,
                p10_net_edge=s.p10_net_edge_per_1000usdt if s is not None else None,
                expected_reuse_label=s.expected_reuse_label if s is not None else None,
                expected_additional_executable_trades=s.expected_additional_executable_trades if s is not None else None,
                reason=f"reconvert to USDT — {reason}",
                simulated=True,
            )
        )

    total_locked = sum(v for v in current_inventory_usdt_value.values() if v > 0)
    remaining_exposure_capacity = max(0.0, max_total_inventory_exposure_usdt - total_locked)
    missing_by_asset = {c.required_base_asset: c for c in missing if c.reason == "INVENTORY_MISSING"}
    remaining_usdt_by_exchange = {"binance": binance_usdt, "bybit": bybit_usdt}

    eligible_sorted = sorted(
        (s for s in scores if is_preposition_eligible(s) and s.base_asset in missing_by_asset),
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
                capital_required_usdt=round(size, 2),
                inventory_score=s.total_score,
                classification=s.classification.value,
                sightings=s.sightings,
                net_positive_rate_pct=s.net_positive_rate_pct,
                median_net_edge=s.median_net_edge_per_1000usdt,
                p10_net_edge=s.p10_net_edge_per_1000usdt,
                expected_reuse_label=s.expected_reuse_label,
                expected_additional_executable_trades=s.expected_additional_executable_trades,
                reason=f"{s.classification.value}: {s.reason} — currently blocking execution on {exchange} (INVENTORY_MISSING)",
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

    # AltcoinScanObservationRecord.observed_at is stored tz-naive; asyncpg
    # rejects comparing it to a tz-aware bound (see
    # app.api.routes.scanner_altcoin_report's own since= convention) —
    # strip tzinfo after computing the UTC-relative window, never before.
    since = (datetime.now(UTC) - lookback).replace(tzinfo=None)
    scan_report = await build_altcoin_scan_report(session, since=since)

    # Informational-only 1h comparison window (item 6/12) — populated onto
    # the short-term summary below, never used to gate classification.
    since_1h = (datetime.now(UTC) - ONE_HOUR_LOOKBACK).replace(tzinfo=None)
    scan_report_1h = await build_altcoin_scan_report(session, since=since_1h)
    mean_1h_by_key = {(s.symbol, s.buy_exchange, s.sell_exchange): s.net_profit_per_1000usdt_mean for s in scan_report_1h.best_direction_by_symbol}

    short_term_regimes = await build_short_term_regimes(session, min_confirmations=settings.min_expected_reuse_count)

    inventory_scores = []
    for s in scan_report.best_direction_by_symbol:
        key = (s.symbol, s.buy_exchange, s.sell_exchange)
        short_term = short_term_regimes.get(key)
        if short_term is not None:
            short_term.mean_net_profit_1h_usdt = mean_1h_by_key.get(key)
        inventory_scores.append(score_direction_for_inventory(s, settings.min_expected_reuse_count, short_term=short_term))
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
