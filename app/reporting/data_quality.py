"""Data Quality (V5/V5.5 Master Orchestration, user directive, 2026-08-22,
spec Part AE).

Reuses app.reporting.simple_summary.build_robot_status's own CEX
per-exchange freshness logic and convention (a feed is LIVE if its most
recent price_snapshots row is within a short staleness window, STALE
otherwise) — extended here with an equivalent per-chain freshness check
for DEX, derived from the same signal DEX detection itself relies on
(most recent opportunities.detected_at per chain), since no separate DEX
price-tick table like CEX's price_snapshots exists to check against.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord
from app.reporting.simple_summary import RobotHealth, build_robot_status

DEX_CHAINS = ("eth", "bsc", "solana")

# Mirrors simple_summary's own CEX staleness convention — DEX polls on a
# much slower cadence (app.onchain.constants.GECKOTERMINAL_MIN_REQUEST_INTERVAL_SECONDS-
# gated, ~45-53s per cycle observed live), so a much longer window than
# CEX's tick-level one is the honest equivalent, not an arbitrary number.
DEX_STALE_AFTER_SECONDS = 180.0
DEX_DEGRADED_AFTER_SECONDS = 90.0


class FeedStatus(StrEnum):
    LIVE = "live"
    DEGRADED = "degraded"
    STALE = "stale"
    UNAVAILABLE = "unavailable"


@dataclass(slots=True)
class DataQualityReport:
    cex_exchanges: dict[str, bool]  # reused as-is from RobotStatus — True means live
    dex_chains: dict[str, FeedStatus]
    cex_last_opportunity_age_seconds: float | None
    overall_health: RobotHealth


async def build_data_quality_report(session: AsyncSession, now: datetime | None = None) -> DataQualityReport:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    robot_status = await build_robot_status(session, now)

    dex_chains: dict[str, FeedStatus] = {}
    for chain in DEX_CHAINS:
        # legs is a JSON array; the chain lives at legs[0]['chain'] — a
        # Postgres JSON path lookup, not a plain column.
        latest = (
            await session.execute(
                select(func.max(OpportunityRecord.detected_at)).where(
                    OpportunityRecord.legs[0]["chain"].as_string() == chain,
                    OpportunityRecord.detected_at >= now - timedelta(hours=6),
                )
            )
        ).scalar()
        if latest is None:
            dex_chains[chain] = FeedStatus.UNAVAILABLE
            continue
        age_seconds = (now - latest).total_seconds()
        if age_seconds <= DEX_DEGRADED_AFTER_SECONDS:
            dex_chains[chain] = FeedStatus.LIVE
        elif age_seconds <= DEX_STALE_AFTER_SECONDS:
            dex_chains[chain] = FeedStatus.DEGRADED
        else:
            dex_chains[chain] = FeedStatus.STALE

    return DataQualityReport(
        cex_exchanges=robot_status.exchanges_connected,
        dex_chains=dex_chains,
        cex_last_opportunity_age_seconds=robot_status.last_opportunity_age_seconds,
        overall_health=robot_status.health,
    )
