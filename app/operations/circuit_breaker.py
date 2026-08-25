"""CIRCUIT BREAKERS PER TYPE (user directive, 2026-08-25, AUTONOMOUS
SELF-HEALING OPERATIONS LAYER, item 7). Four independent scopes -- SYMBOL
(symbol+direction), EXCHANGE, STRATEGY, GLOBAL -- each tracking its own
consecutive-failure count. The user's own example: three deterministic
rejects in a row on one symbol trips only that symbol, not the whole
robot (item 6: "un probleme RVN ne doit pas arreter ZIL, LUNC ou SAND").

Pure state-transition functions; no I/O, no clock reads (every function
takes `now`/`at` as an explicit parameter so this stays deterministic
and trivially testable). Tripping is temporary by design: `is_tripped`
reports False again once `cooldown_until` has passed, matching item 8's
AUTO-RESUME -- but that only means the circuit breaker itself no longer
objects, not that the orchestrator should skip its own revalidation
(fresh market state / balances / reconciliation / kill switch / edge)
before actually resuming that scope."""

from dataclasses import dataclass, replace
from enum import StrEnum


class CircuitBreakerScope(StrEnum):
    SYMBOL = "SYMBOL"
    EXCHANGE = "EXCHANGE"
    STRATEGY = "STRATEGY"
    GLOBAL = "GLOBAL"


DEFAULT_TRIP_THRESHOLD = 3  # "trois rejets deterministes successifs" -- the user's own example
DEFAULT_COOLDOWN_SECONDS = 300.0
GLOBAL_KEY = "GLOBAL"


@dataclass(slots=True, frozen=True)
class ScopeState:
    scope: CircuitBreakerScope
    key: str
    consecutive_failures: int
    tripped: bool
    tripped_at: str | None
    cooldown_until_epoch: float | None


CircuitBreakerState = dict[str, ScopeState]


def _state_key(scope: CircuitBreakerScope, key: str) -> str:
    return f"{scope.value}:{key}"


def record_failure(
    state: CircuitBreakerState, scope: CircuitBreakerScope, key: str, *,
    now_epoch: float, at_iso: str, trip_threshold: int = DEFAULT_TRIP_THRESHOLD,
    cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
) -> CircuitBreakerState:
    """Pure. Returns a new state with one more consecutive failure
    recorded for this scope; trips (and starts the cooldown clock) once
    `trip_threshold` is reached."""
    sk = _state_key(scope, key)
    existing = state.get(sk)
    failures = (existing.consecutive_failures if existing else 0) + 1
    tripped = failures >= trip_threshold
    updated = ScopeState(
        scope=scope, key=key, consecutive_failures=failures, tripped=tripped,
        tripped_at=at_iso if tripped else (existing.tripped_at if existing else None),
        cooldown_until_epoch=(now_epoch + cooldown_seconds) if tripped else (existing.cooldown_until_epoch if existing else None),
    )
    return {**state, sk: updated}


def record_success(state: CircuitBreakerState, scope: CircuitBreakerScope, key: str) -> CircuitBreakerState:
    """Pure. A clean success resets this scope's failure streak
    entirely -- circuit breakers track CONSECUTIVE failures, so any
    intervening success clears the count, exactly like the existing
    CandidateRejectionCache.clear() convention."""
    sk = _state_key(scope, key)
    if sk not in state:
        return state
    return {**state, sk: ScopeState(scope=scope, key=key, consecutive_failures=0, tripped=False, tripped_at=None, cooldown_until_epoch=None)}


def is_tripped(state: CircuitBreakerState, scope: CircuitBreakerScope, key: str, *, now_epoch: float) -> bool:
    """Pure. True only while still within the cooldown window -- once
    `now_epoch` passes `cooldown_until_epoch`, this scope is eligible
    for the orchestrator's own revalidation again (item 8), even though
    the stored `tripped` flag isn't explicitly cleared until the next
    record_success/record_failure call."""
    entry = state.get(_state_key(scope, key))
    if entry is None or not entry.tripped:
        return False
    if entry.cooldown_until_epoch is None:
        return True
    return now_epoch < entry.cooldown_until_epoch


def currently_paused_keys(state: CircuitBreakerState, scope: CircuitBreakerScope, *, now_epoch: float) -> list[str]:
    """For the dashboard's SYMBOLS TEMPORARILY PAUSED / EXCHANGE STATUS
    fields -- every key of this scope still within its cooldown."""
    return sorted(
        entry.key for entry in state.values()
        if entry.scope == scope and is_tripped(state, scope, entry.key, now_epoch=now_epoch)
    )


def global_breaker_tripped(state: CircuitBreakerState, *, now_epoch: float) -> bool:
    return is_tripped(state, CircuitBreakerScope.GLOBAL, GLOBAL_KEY, now_epoch=now_epoch)
