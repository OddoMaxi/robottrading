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


class OpenPositionTracker:
    def __init__(self) -> None:
        self._expiry_by_key: dict[PositionKey, float] = {}

    def is_open(self, key: PositionKey, now: float) -> bool:
        expiry = self._expiry_by_key.get(key)
        return expiry is not None and expiry > now

    def open_position(self, key: PositionKey, now: float, holding_period_seconds: float) -> None:
        self._expiry_by_key[key] = now + holding_period_seconds
