"""DUAL-LEG REALITY VALIDATION reporting (Phase 2F, user directive,
2026-08-23).

Analyzes the persisted dual_leg_observations table (written by
app.database.repository.save_dual_leg_observation, from main.py's CEX
detection loop via app.execution.dual_leg_observer) — the full arbitrage
recomputed independently from live data on BOTH legs, never
opp.expected_profit_usd.

Pure aggregation over already-persisted rows; never fetches market data
itself, never influences what gets persisted, and cannot place an order.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DualLegObservationRecord
from app.reporting.micro_live_edge import DistributionStats, _distribution_stats


def _rejection_bucket(row: DualLegObservationRecord) -> str:
    if not row.buy_tradable:
        return "buy_leg_not_tradable"
    if not row.sell_tradable:
        return "sell_leg_not_tradable"
    if not row.buy_lot_size_pass:
        return "buy_lot_size"
    if not row.sell_lot_size_pass:
        return "sell_lot_size"
    if not row.buy_min_notional_pass:
        return "buy_min_notional"
    if not row.sell_min_notional_pass:
        return "sell_min_notional"
    if float(row.net_profit_usd) <= 0:
        return "net_profit_leq_zero"
    return "other"


@dataclass(slots=True)
class DirectionEdgeStats:
    direction: str  # "binance_to_X" or "X_to_binance"
    observations: int
    net_profit: DistributionStats
    positive_rate_pct: float | None
    real_fee_coverage_pct: float | None


@dataclass(slots=True)
class DualLegEdgeReport:
    observations: int
    window_start: datetime | None
    window_end: datetime | None
    real_fee_coverage_both_legs_pct: float | None
    executable_both_legs_pct: float | None
    net_profit: DistributionStats
    net_return_bps: DistributionStats
    dual_leg_latency_ms: DistributionStats
    rejection_reasons: dict[str, int] = field(default_factory=dict)
    by_direction: list[DirectionEdgeStats] = field(default_factory=list)
    recommended_safety_margin_usd: float = 0.0
    qualifying_after_gate: int = 0
    qualifying_after_gate_pct: float | None = None
    capital_pre_positioning_required: bool = True


def recommend_safety_margin_usd(net_profit_values: list[float]) -> float:
    """Same data-driven methodology as Phase 2E (app.reporting.
    micro_live_edge.recommend_safety_margin_usd): 1 population standard
    deviation of the observed net-profit distribution — never chosen to
    manufacture a favorable result. Dual-leg risk (the latency window
    between observing each leg) is already reflected IN this
    distribution, since every persisted net_profit_usd already accounts
    for the real gap between the two legs' fetch timestamps — a wider
    or more volatile latency window would show up as more spread in this
    same number, not as a separate correction term."""
    if len(net_profit_values) < 2:
        return 0.0
    return round(statistics.pstdev(net_profit_values), 6)


def safety_adjusted_profit_usd(net_profit_usd: float, safety_margin_usd: float) -> float:
    return net_profit_usd - safety_margin_usd


async def _fetch_observations(
    session: AsyncSession, since: datetime | None, until: datetime | None
) -> list[DualLegObservationRecord]:
    stmt = select(DualLegObservationRecord)
    if since is not None:
        stmt = stmt.where(DualLegObservationRecord.observed_at >= since)
    if until is not None:
        stmt = stmt.where(DualLegObservationRecord.observed_at <= until)
    stmt = stmt.order_by(DualLegObservationRecord.observed_at)
    result = await session.execute(stmt)
    return list(result.scalars().all())


def _direction_stats(rows: list[DualLegObservationRecord]) -> list[DirectionEdgeStats]:
    groups: dict[str, list[DualLegObservationRecord]] = {}
    for row in rows:
        direction = f"{row.buy_exchange}_to_{row.sell_exchange}"
        groups.setdefault(direction, []).append(row)

    stats = []
    for direction, group_rows in groups.items():
        net_values = [float(r.net_profit_usd) for r in group_rows]
        both_real_fee = sum(1 for r in group_rows if r.buy_fee_source == "real_account_fee" and r.sell_fee_source == "real_account_fee")
        stats.append(
            DirectionEdgeStats(
                direction=direction,
                observations=len(group_rows),
                net_profit=_distribution_stats(net_values),
                positive_rate_pct=sum(1 for v in net_values if v > 0) / len(net_values) * 100,
                real_fee_coverage_pct=both_real_fee / len(group_rows) * 100,
            )
        )
    return stats


async def build_dual_leg_edge_report(
    session: AsyncSession, since: datetime | None = None, until: datetime | None = None
) -> DualLegEdgeReport:
    rows = await _fetch_observations(session, since, until)

    if not rows:
        return DualLegEdgeReport(
            observations=0,
            window_start=None,
            window_end=None,
            real_fee_coverage_both_legs_pct=None,
            executable_both_legs_pct=None,
            net_profit=_distribution_stats([]),
            net_return_bps=_distribution_stats([]),
            dual_leg_latency_ms=_distribution_stats([]),
        )

    net_values = [float(r.net_profit_usd) for r in rows]
    bps_values = [float(r.net_return_bps) for r in rows]
    latency_values = [float(r.dual_leg_latency_ms) for r in rows]

    both_real_fee_count = sum(1 for r in rows if r.buy_fee_source == "real_account_fee" and r.sell_fee_source == "real_account_fee")
    executable_count = sum(1 for r in rows if r.executable)

    rejection_reasons: dict[str, int] = {}
    for row in rows:
        if not row.executable:
            bucket = _rejection_bucket(row)
            rejection_reasons[bucket] = rejection_reasons.get(bucket, 0) + 1

    safety_margin = recommend_safety_margin_usd(net_values)
    qualifying = sum(1 for v in net_values if v > safety_margin)

    def _naive_to_utc(dt: datetime) -> datetime:
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt

    return DualLegEdgeReport(
        observations=len(rows),
        window_start=_naive_to_utc(rows[0].observed_at),
        window_end=_naive_to_utc(rows[-1].observed_at),
        real_fee_coverage_both_legs_pct=both_real_fee_count / len(rows) * 100,
        executable_both_legs_pct=executable_count / len(rows) * 100,
        net_profit=_distribution_stats(net_values),
        net_return_bps=_distribution_stats(bps_values),
        dual_leg_latency_ms=_distribution_stats(latency_values),
        rejection_reasons=rejection_reasons,
        by_direction=_direction_stats(rows),
        recommended_safety_margin_usd=safety_margin,
        qualifying_after_gate=qualifying,
        qualifying_after_gate_pct=qualifying / len(rows) * 100,
        capital_pre_positioning_required=True,
    )
