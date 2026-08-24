"""FULL MARKET DISCOVERY REPORT (Inventory Manager V2, user directive,
2026-08-24) — item 9/10. Read-only, no order.

Combines three sources: the LIVE dynamic Binance∩Bybit universe size
(app.execution.live_universe), the standalone altcoin_scanner.py
process's latest two-stage-scan counters (FullUniverseScanStatusRecord —
that process runs as its own systemd service with no shared memory with
this one, so the DB row is the only channel), and the persisted
observation history's repeating-edge count + top opportunities
(app.reporting.altcoin_scan_report, already extended with median/P10
edge). Pure aggregation — never influences what altcoin_scanner.py
persists, cannot place an order.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.repository import get_full_universe_scan_status
from app.execution.live_universe import LiveUniverseBuilder, live_universe_builder
from app.reporting.altcoin_scan_report import DirectionSummary, build_altcoin_scan_report

DEFAULT_LOOKBACK_HOURS = 24.0


@dataclass(slots=True)
class FullMarketDiscoveryReport:
    """V2.1 (user directive, 2026-08-24, item 1 "AUDIT IMMÉDIAT DES
    MÉTRIQUES") — pairs_raw_spread_stage_a and pairs_net_positive_stage_b_live
    are named for STAGE + cost-basis + population precisely because they
    are NOT directly comparable: the first is STAGE A's cheap, fee-free
    ESTIMATE over the whole Binance/Bybit universe (pre-cap, before STAGE
    B ever runs); the second is STAGE B's real, fee-adjusted RESULT,
    filtered to market_scope="live" so an OKX-involving result can never
    inflate it (item 2). Comparable in POPULATION now (both Binance/Bybit
    only); still different in STAGE and cost-basis by design — the names
    say so explicitly rather than leaving that to be assumed."""

    common_pairs: int
    pairs_fast_scanned: int  # STAGE A — latest cycle, ~= common_pairs (the whole universe is cheaply checked every cycle)
    pairs_deep_validated: int  # STAGE B — latest cycle, bounded by full_universe_scan_max_stage_b_per_cycle
    pairs_raw_spread_stage_a: int  # STAGE A candidates clearing the raw-spread floor, latest cycle, pre-cap, Binance/Bybit only
    pairs_net_positive_stage_b_live: int  # STAGE B results with real net_profit_usd > 0, latest cycle, market_scope="live" only
    pairs_with_repeating_net_edge: int  # from persisted history over the lookback window, not just the latest cycle
    top_10_opportunities: list[DirectionSummary] = field(default_factory=list)
    scan_status_available: bool = False
    scan_status_age_seconds: float | None = None
    cycle_duration_seconds: float | None = None


async def build_full_market_discovery_report(
    session: AsyncSession,
    universe_builder: LiveUniverseBuilder | None = None,
    min_expected_reuse_count: int = 3,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
) -> FullMarketDiscoveryReport:
    universe_builder = universe_builder or live_universe_builder
    universe = await universe_builder.get_universe()

    status = await get_full_universe_scan_status(session)

    now = datetime.now(UTC).replace(tzinfo=None)
    since = now - timedelta(hours=lookback_hours)
    scan_report = await build_altcoin_scan_report(session, since=since)

    repeating = [
        s
        for s in scan_report.best_direction_by_symbol
        if (s.unique_detections + s.continuations) >= min_expected_reuse_count and s.net_profit_per_1000usdt_mean > 0
    ]
    top10 = sorted(scan_report.best_direction_by_symbol, key=lambda s: s.net_profit_per_1000usdt_mean, reverse=True)[:10]

    return FullMarketDiscoveryReport(
        common_pairs=len(universe.common_symbols),
        pairs_fast_scanned=status.pairs_fast_scanned if status is not None else 0,
        pairs_deep_validated=status.pairs_deep_validated if status is not None else 0,
        pairs_raw_spread_stage_a=status.pairs_raw_spread_stage_a if status is not None else 0,
        pairs_net_positive_stage_b_live=status.pairs_net_positive_stage_b_live if status is not None else 0,
        pairs_with_repeating_net_edge=len(repeating),
        top_10_opportunities=top10,
        scan_status_available=status is not None,
        scan_status_age_seconds=(now - status.updated_at).total_seconds() if status is not None else None,
        cycle_duration_seconds=float(status.cycle_duration_seconds) if status is not None else None,
    )
