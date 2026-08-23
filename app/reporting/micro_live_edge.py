"""REAL EDGE VALIDATION (Phase 2E, user directive, 2026-08-23).

Analyzes the persisted micro_live_observations table (written by
app.database.repository.save_micro_live_observation, from main.py's CEX
detection loop via app.execution.micro_live) to answer the actual
question of this phase: is there a genuinely positive, robust edge at
MICRO_LIVE_CAP=10 USDT after real Binance fees, slippage, book depth, and
rounding — not just "does net_profit clear zero."

Pure aggregation over already-persisted rows — this module never fetches
market data itself and never influences what gets persisted; it only
reads history back. Nothing here can affect paper execution or place an
order.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import MicroLiveObservationRecord
from app.execution.reality_quote import rejection_bucket

LUNC_SYMBOL = "LUNCUSDT"


@dataclass(slots=True)
class DistributionStats:
    count: int
    mean: float | None = None
    median: float | None = None
    p10: float | None = None
    p25: float | None = None
    p50: float | None = None
    p75: float | None = None
    p90: float | None = None
    positive_rate_pct: float | None = None
    negative_rate_pct: float | None = None
    worst: float | None = None
    best: float | None = None
    stdev: float | None = None


def _percentile(sorted_values: list[float], pct: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return sorted_values[0]
    k = (len(sorted_values) - 1) * (pct / 100)
    lo = int(k)
    hi = min(lo + 1, len(sorted_values) - 1)
    if lo == hi:
        return sorted_values[lo]
    return sorted_values[lo] + (sorted_values[hi] - sorted_values[lo]) * (k - lo)


def _distribution_stats(values: list[float]) -> DistributionStats:
    if not values:
        return DistributionStats(count=0)
    s = sorted(values)
    positive = sum(1 for v in values if v > 0)
    negative = sum(1 for v in values if v < 0)
    return DistributionStats(
        count=len(values),
        mean=statistics.fmean(values),
        median=statistics.median(values),
        p10=_percentile(s, 10),
        p25=_percentile(s, 25),
        p50=_percentile(s, 50),
        p75=_percentile(s, 75),
        p90=_percentile(s, 90),
        positive_rate_pct=positive / len(values) * 100,
        negative_rate_pct=negative / len(values) * 100,
        worst=s[0],
        best=s[-1],
        stdev=statistics.pstdev(values) if len(values) > 1 else 0.0,
    )


@dataclass(slots=True)
class GroupEdgeStats:
    key: str  # symbol or strategy name
    observations: int
    net_profit: DistributionStats
    gross_profit_mean: float | None
    avg_fees_usd: float | None
    avg_slippage_pct: float | None
    positive_net_rate_pct: float | None
    real_fee_coverage_pct: float | None


@dataclass(slots=True)
class TimeSliceStats:
    slice_start: datetime
    slice_end: datetime
    observations: int
    positive_net_rate_pct: float | None
    mean_net_profit_usd: float | None
    median_net_profit_usd: float | None


@dataclass(slots=True)
class LuncEdgeReport:
    observations: int
    net_profit: DistributionStats
    avg_book_spread_pct: float | None
    avg_available_depth_usd: float | None
    avg_slippage_pct: float | None
    min_notional_pass_rate_pct: float | None
    lot_size_pass_rate_pct: float | None
    positive_net_rate_pct: float | None
    real_fee_coverage_pct: float | None


@dataclass(slots=True)
class MicroLiveEdgeReport:
    observations: int
    window_start: datetime | None
    window_end: datetime | None
    real_fee_coverage_pct: float | None

    gross_profit: DistributionStats
    net_profit: DistributionStats
    net_return_bps: DistributionStats

    avg_fees_usd: float | None
    avg_slippage_pct: float | None

    rejection_reasons: dict[str, int] = field(default_factory=dict)

    by_symbol: list[GroupEdgeStats] = field(default_factory=list)
    by_strategy: list[GroupEdgeStats] = field(default_factory=list)
    lunc_usdt: LuncEdgeReport | None = None
    time_slices: list[TimeSliceStats] = field(default_factory=list)

    recommended_safety_margin_usd: float = 0.0
    qualifying_after_gate: int = 0

    def top_positive_symbols(self, n: int = 5, min_observations: int = 5) -> list[GroupEdgeStats]:
        eligible = [g for g in self.by_symbol if g.observations >= min_observations and g.net_profit.mean is not None]
        return sorted(eligible, key=lambda g: g.net_profit.mean, reverse=True)[:n]

    def negative_symbols(self, min_observations: int = 5) -> list[GroupEdgeStats]:
        eligible = [g for g in self.by_symbol if g.observations >= min_observations and g.net_profit.mean is not None]
        return sorted([g for g in eligible if g.net_profit.mean < 0], key=lambda g: g.net_profit.mean)


def passes_safety_gate(net_profit_usd: float, safety_margin_usd: float) -> bool:
    """item 7 — a stricter overlay on top of the already-persisted
    executable bit: net_profit_after_all_costs > safety_margin, not just > 0."""
    return net_profit_usd > safety_margin_usd


def recommend_safety_margin_usd(net_profit_values: list[float]) -> float:
    """Data-driven, not chosen to manufacture a result (item 7): one
    population standard deviation of the observed net-profit distribution
    — a positive result must clear the level of noise actually measured
    in this data, not an arbitrary round number picked in advance."""
    if len(net_profit_values) < 2:
        return 0.0
    return round(statistics.pstdev(net_profit_values), 6)


async def _fetch_observations(
    session: AsyncSession, since: datetime | None, until: datetime | None
) -> list[MicroLiveObservationRecord]:
    stmt = select(MicroLiveObservationRecord)
    if since is not None:
        stmt = stmt.where(MicroLiveObservationRecord.observed_at >= since)
    if until is not None:
        stmt = stmt.where(MicroLiveObservationRecord.observed_at <= until)
    stmt = stmt.order_by(MicroLiveObservationRecord.observed_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _group_stats(rows: list[MicroLiveObservationRecord], key_fn) -> list[GroupEdgeStats]:
    groups: dict[str, list[MicroLiveObservationRecord]] = {}
    for row in rows:
        groups.setdefault(key_fn(row), []).append(row)

    stats = []
    for key, group_rows in groups.items():
        net_values = [float(r.net_expected_profit_usd) for r in group_rows]
        real_fee_count = sum(1 for r in group_rows if r.fee_source == "real_binance_fee")
        stats.append(
            GroupEdgeStats(
                key=key,
                observations=len(group_rows),
                net_profit=_distribution_stats(net_values),
                gross_profit_mean=statistics.fmean(float(r.gross_expected_profit_usd) for r in group_rows),
                avg_fees_usd=statistics.fmean(float(r.estimated_fees_usd) for r in group_rows),
                avg_slippage_pct=statistics.fmean(float(r.estimated_slippage_pct) for r in group_rows),
                positive_net_rate_pct=sum(1 for v in net_values if v > 0) / len(net_values) * 100,
                real_fee_coverage_pct=real_fee_count / len(group_rows) * 100,
            )
        )
    return stats


def _time_slices(rows: list[MicroLiveObservationRecord], slice_minutes: float) -> list[TimeSliceStats]:
    if not rows:
        return []
    slices: list[TimeSliceStats] = []
    slice_delta = timedelta(minutes=slice_minutes)
    start = rows[0].observed_at
    end_overall = rows[-1].observed_at
    cursor = start
    while cursor <= end_overall:
        slice_end = cursor + slice_delta
        bucket = [r for r in rows if cursor <= r.observed_at < slice_end]
        if bucket:
            net_values = [float(r.net_expected_profit_usd) for r in bucket]
            slices.append(
                TimeSliceStats(
                    slice_start=cursor,
                    slice_end=slice_end,
                    observations=len(bucket),
                    positive_net_rate_pct=sum(1 for v in net_values if v > 0) / len(net_values) * 100,
                    mean_net_profit_usd=statistics.fmean(net_values),
                    median_net_profit_usd=statistics.median(net_values),
                )
            )
        cursor = slice_end
    return slices


async def build_micro_live_edge_report(
    session: AsyncSession,
    since: datetime | None = None,
    until: datetime | None = None,
    slice_minutes: float = 30.0,
) -> MicroLiveEdgeReport:
    rows = await _fetch_observations(session, since, until)

    if not rows:
        return MicroLiveEdgeReport(
            observations=0,
            window_start=None,
            window_end=None,
            real_fee_coverage_pct=None,
            gross_profit=_distribution_stats([]),
            net_profit=_distribution_stats([]),
            net_return_bps=_distribution_stats([]),
            avg_fees_usd=None,
            avg_slippage_pct=None,
        )

    net_values = [float(r.net_expected_profit_usd) for r in rows]
    gross_values = [float(r.gross_expected_profit_usd) for r in rows]
    bps_values = [float(r.net_return_bps) for r in rows]
    real_fee_count = sum(1 for r in rows if r.fee_source == "real_binance_fee")

    rejection_reasons: dict[str, int] = {}
    for row in rows:
        if not row.executable:
            bucket = rejection_bucket(row.min_notional_pass, row.lot_size_pass, row.balance_pass, float(row.net_expected_profit_usd))
            rejection_reasons[bucket] = rejection_reasons.get(bucket, 0) + 1

    lunc_rows = [r for r in rows if r.symbol == LUNC_SYMBOL]
    lunc_report = None
    if lunc_rows:
        lunc_net = [float(r.net_expected_profit_usd) for r in lunc_rows]
        lunc_real_fee_count = sum(1 for r in lunc_rows if r.fee_source == "real_binance_fee")
        lunc_report = LuncEdgeReport(
            observations=len(lunc_rows),
            net_profit=_distribution_stats(lunc_net),
            avg_book_spread_pct=statistics.fmean(float(r.book_spread_pct) for r in lunc_rows),
            avg_available_depth_usd=statistics.fmean(float(r.available_depth_usd) for r in lunc_rows),
            avg_slippage_pct=statistics.fmean(float(r.estimated_slippage_pct) for r in lunc_rows),
            min_notional_pass_rate_pct=sum(1 for r in lunc_rows if r.min_notional_pass) / len(lunc_rows) * 100,
            lot_size_pass_rate_pct=sum(1 for r in lunc_rows if r.lot_size_pass) / len(lunc_rows) * 100,
            positive_net_rate_pct=sum(1 for v in lunc_net if v > 0) / len(lunc_net) * 100,
            real_fee_coverage_pct=lunc_real_fee_count / len(lunc_rows) * 100,
        )

    safety_margin = recommend_safety_margin_usd(net_values)
    qualifying = sum(1 for v in net_values if passes_safety_gate(v, safety_margin))

    return MicroLiveEdgeReport(
        observations=len(rows),
        window_start=rows[0].observed_at.replace(tzinfo=UTC) if rows[0].observed_at.tzinfo is None else rows[0].observed_at,
        window_end=rows[-1].observed_at.replace(tzinfo=UTC) if rows[-1].observed_at.tzinfo is None else rows[-1].observed_at,
        real_fee_coverage_pct=real_fee_count / len(rows) * 100,
        gross_profit=_distribution_stats(gross_values),
        net_profit=_distribution_stats(net_values),
        net_return_bps=_distribution_stats(bps_values),
        avg_fees_usd=statistics.fmean(float(r.estimated_fees_usd) for r in rows),
        avg_slippage_pct=statistics.fmean(float(r.estimated_slippage_pct) for r in rows),
        rejection_reasons=rejection_reasons,
        by_symbol=_group_stats(rows, lambda r: r.symbol),
        by_strategy=_group_stats(rows, lambda r: r.strategy),
        lunc_usdt=lunc_report,
        time_slices=_time_slices(rows, slice_minutes),
        recommended_safety_margin_usd=safety_margin,
        qualifying_after_gate=qualifying,
    )
