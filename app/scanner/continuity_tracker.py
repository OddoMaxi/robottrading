"""NEW vs CONTINUATION + spread-persistence tracking (user directive,
2026-08-23) — mirrors the NEW_OPPORTUNITY vs CONTINUATION distinction
already established for the real engines (app.opportunity.tracker,
Phase 2B's CEX scan-level telemetry) applied here to
(symbol, buy_exchange, sell_exchange) keys instead of a single
opportunity id — since this scanner evaluates every direction on every
cycle rather than tracking a single detected Opportunity object.

Pure in-memory state, process-local to altcoin_scanner.py — never shared
with, or read by, main.py's real detection/execution state.
"""

import time
from dataclasses import dataclass


@dataclass(slots=True)
class DirectionStreak:
    is_positive: bool = False
    streak_started_at: float | None = None
    last_seen_positive_at: float | None = None
    detections: int = 0  # count of NEW streak starts
    continuations: int = 0  # count of ticks that extended an existing streak
    total_positive_ticks: int = 0
    longest_streak_seconds: float = 0.0


class ContinuityTracker:
    def __init__(self) -> None:
        self._streaks: dict[tuple[str, str, str], DirectionStreak] = {}

    def observe(self, symbol: str, buy_exchange: str, sell_exchange: str, is_executable_and_positive: bool, now: float | None = None) -> str:
        """Records one tick for this direction; returns 'new',
        'continuation', or 'none' (spread not currently positive)."""
        now = now if now is not None else time.time()
        key = (symbol, buy_exchange, sell_exchange)
        streak = self._streaks.setdefault(key, DirectionStreak())

        if not is_executable_and_positive:
            if streak.is_positive and streak.streak_started_at is not None:
                duration = (streak.last_seen_positive_at or streak.streak_started_at) - streak.streak_started_at
                streak.longest_streak_seconds = max(streak.longest_streak_seconds, duration)
            streak.is_positive = False
            streak.streak_started_at = None
            return "none"

        streak.total_positive_ticks += 1
        if streak.is_positive:
            streak.continuations += 1
            streak.last_seen_positive_at = now
            return "continuation"

        streak.is_positive = True
        streak.streak_started_at = now
        streak.last_seen_positive_at = now
        streak.detections += 1
        return "new"

    def current_persistence_seconds(self, symbol: str, buy_exchange: str, sell_exchange: str, now: float | None = None) -> float:
        now = now if now is not None else time.time()
        streak = self._streaks.get((symbol, buy_exchange, sell_exchange))
        if streak is None or not streak.is_positive or streak.streak_started_at is None:
            return 0.0
        return now - streak.streak_started_at

    def summary(self, symbol: str, buy_exchange: str, sell_exchange: str) -> DirectionStreak:
        return self._streaks.get((symbol, buy_exchange, sell_exchange), DirectionStreak())

    def all_summaries(self) -> dict[tuple[str, str, str], DirectionStreak]:
        return dict(self._streaks)
