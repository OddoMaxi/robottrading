"""TOP CROSS-EXCHANGE OPPORTUNITIES — LIVE (user directive, 2026-08-23).

Aggregates the persisted altcoin_scan_observations table (written by the
standalone altcoin_scanner.py process) into a per-symbol summary with a
STRONG/WATCH/WEAK/NO_EDGE status and a MARKET_PRIORITY_SCORE — explicitly
NOT based on 24h price change: volatility here only ever means "how much
the cross-exchange net spread itself moves," never a directional price
move. A symbol pumping 30% in an hour with the same spread on every
exchange is exactly as arbitrage-uninteresting as one that's flat.

Pure aggregation over already-persisted rows; never fetches market data
itself, never influences what gets persisted, and cannot place an order.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AltcoinScanObservationRecord

LUNC_SYMBOL = "LUNC/USDT"

# Thresholds are round, stated numbers chosen up front — not fitted to
# make any particular symbol look good. Recalibrate only with a stated
# reason, never to flip a specific symbol's status.
MIN_OBSERVATIONS_TO_JUDGE = 5
STRONG_POSITIVE_RATE_PCT = 70.0
WATCH_POSITIVE_RATE_PCT = 30.0
STRONG_MIN_NET_PROFIT_PER_1000USDT = 2.0  # 20 bps — comfortably above the ~10-20bps combined real fee floor typically observed
STRONG_MIN_PERSISTENCE_SECONDS = 20.0  # long enough that a real order (seconds of latency) could plausibly land inside the window


class OpportunityStatus(StrEnum):
    STRONG = "STRONG"
    WATCH = "WATCH"
    WEAK = "WEAK"
    NO_EDGE = "NO_EDGE"


@dataclass(slots=True)
class DirectionSummary:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    observations: int
    gross_spread_mean_pct: float
    gross_spread_max_pct: float
    net_spread_mean_pct: float
    net_spread_max_pct: float
    net_profit_per_1000usdt_mean: float
    net_profit_per_1000usdt_max: float
    positive_rate_pct: float
    mean_persistence_seconds: float
    max_persistence_seconds: float
    unique_detections: int
    continuations: int
    best_observed_at: datetime
    status: OpportunityStatus


@dataclass(slots=True)
class AltcoinScanReport:
    window_start: datetime | None
    window_end: datetime | None
    total_observations: int
    best_direction_by_symbol: list[DirectionSummary] = field(default_factory=list)
    lunc_benchmark: DirectionSummary | None = None
    better_than_lunc: list[str] = field(default_factory=list)


def _classify(positive_rate_pct: float, net_profit_per_1000usdt_mean: float, mean_persistence_seconds: float, observations: int) -> OpportunityStatus:
    if observations < MIN_OBSERVATIONS_TO_JUDGE:
        return OpportunityStatus.NO_EDGE
    if positive_rate_pct <= 0:
        return OpportunityStatus.NO_EDGE
    if (
        positive_rate_pct >= STRONG_POSITIVE_RATE_PCT
        and net_profit_per_1000usdt_mean >= STRONG_MIN_NET_PROFIT_PER_1000USDT
        and mean_persistence_seconds >= STRONG_MIN_PERSISTENCE_SECONDS
    ):
        return OpportunityStatus.STRONG
    if positive_rate_pct >= WATCH_POSITIVE_RATE_PCT:
        return OpportunityStatus.WATCH
    return OpportunityStatus.WEAK


def _summarize_direction(rows: list[AltcoinScanObservationRecord]) -> DirectionSummary:
    gross = [float(r.gross_spread_pct) for r in rows]
    net_pct = [float(r.net_return_bps) / 100 for r in rows]  # bps -> pct, for a like-for-like "net spread %" figure
    per_1000 = [float(r.net_profit_per_1000usdt) for r in rows]
    positive = [r for r in rows if r.executable and float(r.net_profit_usd) > 0]
    persistence = [float(r.persistence_seconds) for r in rows if r.continuity_status in ("new", "continuation")]
    detections = sum(1 for r in rows if r.continuity_status == "new")
    continuations = sum(1 for r in rows if r.continuity_status == "continuation")
    best_row = max(rows, key=lambda r: float(r.net_profit_usd))

    positive_rate = len(positive) / len(rows) * 100 if rows else 0.0
    mean_persistence = statistics.fmean(persistence) if persistence else 0.0
    max_persistence = max(persistence) if persistence else 0.0
    mean_per_1000 = statistics.fmean(per_1000) if per_1000 else 0.0

    return DirectionSummary(
        symbol=rows[0].symbol,
        buy_exchange=rows[0].buy_exchange,
        sell_exchange=rows[0].sell_exchange,
        observations=len(rows),
        gross_spread_mean_pct=statistics.fmean(gross) if gross else 0.0,
        gross_spread_max_pct=max(gross) if gross else 0.0,
        net_spread_mean_pct=statistics.fmean(net_pct) if net_pct else 0.0,
        net_spread_max_pct=max(net_pct) if net_pct else 0.0,
        net_profit_per_1000usdt_mean=mean_per_1000,
        net_profit_per_1000usdt_max=max(per_1000) if per_1000 else 0.0,
        positive_rate_pct=positive_rate,
        mean_persistence_seconds=mean_persistence,
        max_persistence_seconds=max_persistence,
        unique_detections=detections,
        continuations=continuations,
        best_observed_at=best_row.observed_at.replace(tzinfo=UTC) if best_row.observed_at.tzinfo is None else best_row.observed_at,
        status=_classify(positive_rate, mean_per_1000, mean_persistence, len(rows)),
    )


async def _fetch_observations(session: AsyncSession, since: datetime | None, until: datetime | None) -> list[AltcoinScanObservationRecord]:
    stmt = select(AltcoinScanObservationRecord)
    if since is not None:
        stmt = stmt.where(AltcoinScanObservationRecord.observed_at >= since)
    if until is not None:
        stmt = stmt.where(AltcoinScanObservationRecord.observed_at <= until)
    stmt = stmt.order_by(AltcoinScanObservationRecord.observed_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _group_by_symbol_and_direction(rows: list[AltcoinScanObservationRecord]) -> dict[tuple[str, str, str], list[AltcoinScanObservationRecord]]:
    groups: dict[tuple[str, str, str], list[AltcoinScanObservationRecord]] = {}
    for row in rows:
        groups.setdefault((row.symbol, row.buy_exchange, row.sell_exchange), []).append(row)
    return groups


def _best_direction_per_symbol(groups: dict[tuple[str, str, str], list[AltcoinScanObservationRecord]]) -> list[DirectionSummary]:
    by_symbol: dict[str, list[DirectionSummary]] = {}
    for (symbol, _buy, _sell), rows in groups.items():
        by_symbol.setdefault(symbol, []).append(_summarize_direction(rows))
    best: list[DirectionSummary] = []
    for symbol, summaries in by_symbol.items():
        best.append(max(summaries, key=lambda s: s.net_profit_per_1000usdt_mean))
    return best


def market_priority_score(summary: DirectionSummary, gross_spread_volatility_pct: float) -> float:
    """MARKET_PRIORITY_SCORE = volatility_score * liquidity_score *
    net_spread_score * persistence_score * opportunity_frequency_score —
    each sub-score normalized to roughly [0, 1] so the product stays
    interpretable, and 'volatility' here means spread volatility (how
    much the CROSS-EXCHANGE GAP itself moves), never the 24h price
    change — a large price move with an identical spread on every
    exchange scores ~0 here, by design."""
    volatility_score = min(1.0, gross_spread_volatility_pct / 1.0)  # 1%+ stdev in the gross spread itself is already a lot
    net_spread_score = min(1.0, max(0.0, summary.net_profit_per_1000usdt_mean / STRONG_MIN_NET_PROFIT_PER_1000USDT))
    persistence_score = min(1.0, summary.mean_persistence_seconds / STRONG_MIN_PERSISTENCE_SECONDS) if summary.mean_persistence_seconds > 0 else 0.0
    frequency_score = min(1.0, summary.unique_detections / 10.0)
    liquidity_score = 1.0 if summary.observations >= MIN_OBSERVATIONS_TO_JUDGE else summary.observations / MIN_OBSERVATIONS_TO_JUDGE
    return volatility_score * liquidity_score * net_spread_score * persistence_score * frequency_score


async def build_altcoin_scan_report(session: AsyncSession, since: datetime | None = None, until: datetime | None = None) -> AltcoinScanReport:
    rows = await _fetch_observations(session, since, until)
    if not rows:
        return AltcoinScanReport(window_start=None, window_end=None, total_observations=0)

    groups = _group_by_symbol_and_direction(rows)
    best_by_symbol = _best_direction_per_symbol(groups)
    best_by_symbol.sort(key=lambda s: s.net_profit_per_1000usdt_mean, reverse=True)

    lunc_candidates = [s for s in best_by_symbol if s.symbol == LUNC_SYMBOL]
    lunc_benchmark = lunc_candidates[0] if lunc_candidates else None

    better_than_lunc = []
    if lunc_benchmark is not None:
        better_than_lunc = [
            s.symbol
            for s in best_by_symbol
            if s.symbol != LUNC_SYMBOL and s.net_profit_per_1000usdt_mean > lunc_benchmark.net_profit_per_1000usdt_mean
        ]

    def _naive_to_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    return AltcoinScanReport(
        window_start=_naive_to_utc(rows[0].observed_at),
        window_end=_naive_to_utc(rows[-1].observed_at),
        total_observations=len(rows),
        best_direction_by_symbol=best_by_symbol,
        lunc_benchmark=lunc_benchmark,
        better_than_lunc=better_than_lunc,
    )
