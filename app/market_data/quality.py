"""Market Data Quality Engine (Reality Engine spec, section 5).

Classifies each market data feed's health independently of the others — a
spread built from a HEALTHY spot feed and a STALE funding feed isn't real,
even though the spot side looks perfectly fine on its own. This is the gap
that let funding/basis opportunities price off an arbitrarily old funding
or delivery-futures reading with no staleness check at all (only the spot
leg was ever checked) — found auditing the engines for this spec.

A feed's "normal" age varies a lot by source: a WS tick stream updates
sub-second, while funding/delivery-futures snapshots are REST-polled every
30-60s by design (see app/collectors/binance/funding.py,
app/collectors/binance/basis_futures.py) — comparing them against one
fixed staleness constant would either reject valid funding data
constantly or let a genuinely dead spot feed through. Thresholds are
expressed as multiples of each feed's own expected cadence instead.
"""

import time
from dataclasses import dataclass
from enum import StrEnum


class FeedHealth(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    STALE = "stale"
    BROKEN = "broken"


# Multiples of a feed's own expected update cadence. A funding feed polled
# every 30s isn't stale at 35s (just unlucky timing) but is stale at 150s
# (5 missed polls in a row — something's actually wrong).
DEGRADED_AFTER_CADENCE_MULTIPLES = 2.0
STALE_AFTER_CADENCE_MULTIPLES = 4.0
BROKEN_AFTER_CADENCE_MULTIPLES = 10.0

# Expected update cadence per feed type, in seconds — matches each
# collector's own polling interval (WS tick streams are ~continuous).
SPOT_TICK_CADENCE_SECONDS = 2.0
FUNDING_POLL_CADENCE_SECONDS = 30.0
DELIVERY_FUTURES_POLL_CADENCE_SECONDS = 60.0


def classify_feed_health(age_seconds: float | None, expected_cadence_seconds: float) -> FeedHealth:
    if age_seconds is None:
        return FeedHealth.BROKEN
    if age_seconds > expected_cadence_seconds * BROKEN_AFTER_CADENCE_MULTIPLES:
        return FeedHealth.BROKEN
    if age_seconds > expected_cadence_seconds * STALE_AFTER_CADENCE_MULTIPLES:
        return FeedHealth.STALE
    if age_seconds > expected_cadence_seconds * DEGRADED_AFTER_CADENCE_MULTIPLES:
        return FeedHealth.DEGRADED
    return FeedHealth.HEALTHY


def blocks_new_execution(health: FeedHealth) -> bool:
    """Section 5: STALE or BROKEN means NO NEW EXECUTION."""
    return health in (FeedHealth.STALE, FeedHealth.BROKEN)


@dataclass(slots=True)
class FeedStatus:
    exchange: str
    symbol: str
    market_type: str
    last_update_at: float | None
    age_seconds: float | None
    expected_cadence_seconds: float
    health: FeedHealth


def build_feed_status(
    exchange: str,
    symbol: str,
    market_type: str,
    last_update_at: float | None,
    expected_cadence_seconds: float,
    now: float | None = None,
) -> FeedStatus:
    now = now if now is not None else time.time()
    age_seconds = max(0.0, now - last_update_at) if last_update_at is not None else None
    return FeedStatus(
        exchange=exchange,
        symbol=symbol,
        market_type=market_type,
        last_update_at=last_update_at,
        age_seconds=age_seconds,
        expected_cadence_seconds=expected_cadence_seconds,
        health=classify_feed_health(age_seconds, expected_cadence_seconds),
    )
