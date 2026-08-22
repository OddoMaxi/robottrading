"""Shadow Open-Position Tracker (Phase 2, SHADOW MODE ONLY — corrective
maintenance #2, user directive, 2026-08-22).

FIX: MASTER previously had no concept of "this (strategy, exchange,
symbol) already has a position open" — the exact gate
app.execution.validator.validate() uses (via
app.simulation.position_tracker.OpenPositionTracker) to reject
re-detections of a persisting CEX opportunity while its holding period
hasn't elapsed yet. Without it, MASTER independently "allocated" to the
SAME persisting cross_exchange spread dozens of times per hour — the
root cause of the near-zero (6.1%) OLD-vs-MASTER agreement rate on
cross_exchange found in the first Shadow Mode validation.

Deliberately a small, standalone reimplementation of
OpenPositionTracker's exact same shape and semantics (same
DEFAULT_MIN_REENTRY_DELAY_SECONDS) — NOT an import of
app.simulation.position_tracker, for the same isolation reason
app.shadow.ledger doesn't import app.onchain.dex_paper_trader's
DexCapitalPool (see app/shadow/__init__.py,
tests/test_shadow_isolation.py). Reproducing the real economic rule
(one open position per key at a time) does not require reusing the real
object that could, if imported, also carry the real engine's live state.
"""

PositionKey = tuple[str, str, str]  # (strategy, exchange, symbol) — identical shape to app.simulation.position_tracker.PositionKey

# Matches app.simulation.position_tracker.DEFAULT_MIN_REENTRY_DELAY_SECONDS
# exactly — MASTER must reproduce the SAME economic rule the real gate
# uses, not a looser or stricter approximation of it.
DEFAULT_MIN_REENTRY_DELAY_SECONDS = 0.5


class ShadowOpenPositionTracker:
    def __init__(self, min_reentry_delay_seconds: float = DEFAULT_MIN_REENTRY_DELAY_SECONDS) -> None:
        self._expiry_by_key: dict[PositionKey, float] = {}
        self._min_reentry_delay_seconds = min_reentry_delay_seconds

    def is_open(self, key: PositionKey, now: float) -> bool:
        expiry = self._expiry_by_key.get(key)
        return expiry is not None and expiry > now

    def open_position(self, key: PositionKey, now: float, holding_period_seconds: float) -> None:
        self._expiry_by_key[key] = now + holding_period_seconds + self._min_reentry_delay_seconds


def position_key_for(strategy: str, legs: list[dict], symbol: str) -> PositionKey | None:
    """Identical derivation to app.execution.validator.validate()'s own
    `(opp.strategy, opp.legs[0].get("exchange"), opp.symbol)` — copied
    exactly, not reinvented, so MASTER blocks on the SAME key OLD does."""
    if not legs:
        return None
    exchange = legs[0].get("exchange")
    if exchange is None:
        return None
    return (strategy, exchange, symbol)
