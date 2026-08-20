"""Tracks "currently open" positions for hold-based strategies (Basis,
Funding) so the same persistent opportunity — re-detected on every scan,
sometimes several times a second — isn't paper-traded as if it were a new,
independent trade each time.

A basis or funding position, once opened, ties up capital for its whole
holding period; only one can realistically be open per (strategy, exchange,
symbol) at a time. Without this, a 36-day basis trade that never actually
changes gets "opened" thousands of times, each crediting a full trade's
worth of profit to the portfolio — a real bug found in production: a $1,000
portfolio showed $52,614 of cumulative simulated basis profit from 21,640
paper trades of the same underlying position.
"""

PositionKey = tuple[str, str, str]  # (strategy, exchange, symbol)

# Continuous Execution spec, section 10 — a small cooldown after a position
# closes before the same key can be re-traded, even in Fast Rotation Mode.
# Without it, a position could reopen the instant its holding period
# elapses (0ms gap), which no real execution pipeline could actually
# achieve back-to-back.
DEFAULT_MIN_REENTRY_DELAY_SECONDS = 0.5


class OpenPositionTracker:
    def __init__(self, min_reentry_delay_seconds: float = DEFAULT_MIN_REENTRY_DELAY_SECONDS) -> None:
        self._expiry_by_key: dict[PositionKey, float] = {}
        self._min_reentry_delay_seconds = min_reentry_delay_seconds

    def is_open(self, key: PositionKey, now: float) -> bool:
        expiry = self._expiry_by_key.get(key)
        return expiry is not None and expiry > now

    def open_position(self, key: PositionKey, now: float, holding_period_seconds: float) -> None:
        self._expiry_by_key[key] = now + holding_period_seconds + self._min_reentry_delay_seconds
