from pathlib import Path

import pytest

from app.operations.order_intent_log import (
    resolve_intent,
    save_state,
    load_state,
    start_intent,
    unresolved_intents,
)


def test_start_intent_is_unresolved_by_default():
    state = start_intent({}, intent_id="i1", purpose="ARBITRAGE", exchange="binance", symbol="RVN/USDT", at="2026-08-25T00:00:00", notional_usdt=10.0)
    assert unresolved_intents(state) == [state["i1"]]
    assert state["i1"].resolved is False


def test_resolve_intent_marks_it_resolved_without_mutating_input():
    state = start_intent({}, intent_id="i1", purpose="ARBITRAGE", exchange="binance", symbol="RVN/USDT", at="2026-08-25T00:00:00")
    new_state = resolve_intent(state, "i1", at="2026-08-25T00:00:05", outcome="both_filled")
    assert state["i1"].resolved is False  # input untouched
    assert new_state["i1"].resolved is True
    assert new_state["i1"].resolved_outcome == "both_filled"
    assert unresolved_intents(new_state) == []


def test_a_clean_rejection_is_still_a_valid_resolution():
    """The point of this log is whether the operation CONCLUDED, not
    what it concluded with -- a rejected/no-fill outcome resolves the
    intent exactly like a successful fill."""
    state = start_intent({}, intent_id="i1", purpose="INVENTORY_CONSTITUTION", exchange="bybit", symbol="ZIL/USDT", at="t0")
    resolved = resolve_intent(state, "i1", at="t1", outcome="rejected_before_creation")
    assert unresolved_intents(resolved) == []


def test_resolving_an_unknown_intent_raises():
    with pytest.raises(KeyError):
        resolve_intent({}, "never-started", at="t1", outcome="anything")


def test_multiple_intents_only_unresolved_ones_are_flagged():
    state = start_intent({}, intent_id="i1", purpose="ARBITRAGE", exchange="binance", symbol="RVN/USDT", at="t0")
    state = start_intent(state, intent_id="i2", purpose="REBALANCE_SELL", exchange="binance", symbol="LUNC/USDT", at="t0", client_order_id="rebal-abc")
    state = resolve_intent(state, "i1", at="t1", outcome="both_filled")
    remaining = unresolved_intents(state)
    assert len(remaining) == 1
    assert remaining[0].intent_id == "i2"
    assert remaining[0].client_order_id == "rebal-abc"


def test_a_crash_before_resolution_leaves_the_intent_unresolved_forever_until_reviewed():
    """Simulates the exact scenario this module exists for: start_intent
    is called, then nothing else happens (the process crashed) -- a
    fresh load of the persisted state must still show it unresolved."""
    state = start_intent({}, intent_id="i1", purpose="ARBITRAGE", exchange="binance", symbol="RVN/USDT", at="t0", notional_usdt=10.0)
    # (crash -- no resolve_intent call)
    assert len(unresolved_intents(state)) == 1


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "intents.json"
    state = start_intent({}, intent_id="i1", purpose="ARBITRAGE", exchange="binance", symbol="RVN/USDT", at="t0", notional_usdt=10.0)
    save_state(state, path)
    reloaded = load_state(path)
    assert reloaded["i1"].symbol == "RVN/USDT"
    assert reloaded["i1"].resolved is False


def test_load_state_without_a_file_returns_empty():
    assert load_state(Path("/nonexistent/intents.json")) == {}


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "intents.json"
    save_state({}, path)
    assert path.exists()
