from app.operations.circuit_breaker import (
    GLOBAL_KEY,
    CircuitBreakerScope,
    currently_paused_keys,
    global_breaker_tripped,
    is_tripped,
    record_failure,
    record_success,
)

NOW = 1_000_000.0


def _fail(state, scope, key, n, *, now=NOW, threshold=3, cooldown=300.0):
    for _ in range(n):
        state = record_failure(state, scope, key, now_epoch=now, at_iso="2026-08-25T00:00:00", trip_threshold=threshold, cooldown_seconds=cooldown)
    return state


def test_below_threshold_does_not_trip():
    state = _fail({}, CircuitBreakerScope.SYMBOL, "RVN/USDT:BINANCE_BUY_BYBIT_SELL", 2)
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:BINANCE_BUY_BYBIT_SELL", now_epoch=NOW) is False


def test_reaching_threshold_trips():
    state = _fail({}, CircuitBreakerScope.SYMBOL, "RVN/USDT:BINANCE_BUY_BYBIT_SELL", 3)
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:BINANCE_BUY_BYBIT_SELL", now_epoch=NOW) is True


def test_a_problem_on_one_symbol_does_not_trip_another():
    """The user's own item-6 example: RVN failing must not pause ZIL."""
    state = _fail({}, CircuitBreakerScope.SYMBOL, "RVN/USDT:BINANCE_BUY_BYBIT_SELL", 3)
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "ZIL/USDT:BINANCE_BUY_BYBIT_SELL", now_epoch=NOW) is False


def test_success_resets_the_consecutive_streak():
    state = _fail({}, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", 2)
    state = record_success(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:X")
    state = _fail(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", 2)  # only 2 more -- would trip at 3 if not reset
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", now_epoch=NOW) is False


def test_tripped_scope_auto_expires_after_cooldown():
    state = _fail({}, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", 3, cooldown=100.0)
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", now_epoch=NOW + 50) is True
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", now_epoch=NOW + 150) is False


def test_exchange_scope_is_independent_of_symbol_scope():
    state = _fail({}, CircuitBreakerScope.EXCHANGE, "bybit", 3)
    assert is_tripped(state, CircuitBreakerScope.EXCHANGE, "bybit", now_epoch=NOW) is True
    assert is_tripped(state, CircuitBreakerScope.SYMBOL, "bybit", now_epoch=NOW) is False  # different scope, same key string


def test_global_breaker_helper():
    state = _fail({}, CircuitBreakerScope.GLOBAL, GLOBAL_KEY, 3)
    assert global_breaker_tripped(state, now_epoch=NOW) is True


def test_currently_paused_keys_lists_only_tripped_within_cooldown():
    state = _fail({}, CircuitBreakerScope.SYMBOL, "RVN/USDT:X", 3, cooldown=100.0)
    state = _fail(state, CircuitBreakerScope.SYMBOL, "ZIL/USDT:X", 3, cooldown=100.0)
    state = _fail(state, CircuitBreakerScope.SYMBOL, "SAND/USDT:X", 2)  # below threshold
    paused_now = currently_paused_keys(state, CircuitBreakerScope.SYMBOL, now_epoch=NOW + 50)
    assert paused_now == ["RVN/USDT:X", "ZIL/USDT:X"]
    paused_later = currently_paused_keys(state, CircuitBreakerScope.SYMBOL, now_epoch=NOW + 150)
    assert paused_later == []


def test_record_success_on_a_never_seen_key_is_a_noop():
    state = record_success({}, CircuitBreakerScope.SYMBOL, "NEVER/SEEN")
    assert state == {}


def test_failure_count_persists_across_calls_until_tripped_or_reset():
    state = _fail({}, CircuitBreakerScope.STRATEGY, "arbitrage", 1)
    state = _fail(state, CircuitBreakerScope.STRATEGY, "arbitrage", 1)
    assert is_tripped(state, CircuitBreakerScope.STRATEGY, "arbitrage", now_epoch=NOW) is False
    state = _fail(state, CircuitBreakerScope.STRATEGY, "arbitrage", 1)
    assert is_tripped(state, CircuitBreakerScope.STRATEGY, "arbitrage", now_epoch=NOW) is True
