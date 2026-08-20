"""Holding-time classification (Fast-Rotation spec, sections 1 & 12).

Every strategy is bucketed by how long it ties up capital, independent of
which engine produced it — this is what separates FAST MODE (the new
default: capital recycled in seconds to minutes) from CARRY MODE (the old
model: capital locked for days to weeks, e.g. Basis/Funding).
"""

from app.config.constants import (
    FAST_MAX_SECONDS,
    MEDIUM_MAX_SECONDS,
    ULTRA_FAST_MAX_SECONDS,
    HoldingTimeCategory,
)


def classify_holding_time(holding_period_seconds: float) -> HoldingTimeCategory:
    if holding_period_seconds < ULTRA_FAST_MAX_SECONDS:
        return HoldingTimeCategory.ULTRA_FAST
    if holding_period_seconds < FAST_MAX_SECONDS:
        return HoldingTimeCategory.FAST
    if holding_period_seconds < MEDIUM_MAX_SECONDS:
        return HoldingTimeCategory.MEDIUM
    return HoldingTimeCategory.CARRY


def is_fast_mode(holding_period_seconds: float) -> bool:
    """Fast Mode = anything under the spec's 30-minute Maximum Holding Time (section 11)."""
    return classify_holding_time(holding_period_seconds) != HoldingTimeCategory.CARRY
