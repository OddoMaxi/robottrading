import json
from pathlib import Path

from app.operations.persistent_kill_switch import (
    KillSwitchState,
    clear_kill_switch,
    engage,
    load_state,
    save_state,
)


def test_missing_file_means_never_engaged():
    state = load_state(Path("/nonexistent/kill_switch.json"))
    assert state.engaged is False


def test_engage_builds_the_correct_record():
    state = engage(reason="NEUTRALIZATION FAILED", at="2026-08-25T00:00:00", incident={"type": "NEUTRALIZATION FAILED"})
    assert state.engaged is True
    assert state.reason == "NEUTRALIZATION FAILED"
    assert state.incident == {"type": "NEUTRALIZATION FAILED"}


def test_save_then_load_round_trips_an_engaged_state(tmp_path):
    path = tmp_path / "ks.json"
    state = engage(reason="CAPITAL SAFETY VIOLATION", at="2026-08-25T01:00:00", incident={"type": "CAPITAL SAFETY VIOLATION", "scan": 12})
    save_state(state, path)
    reloaded = load_state(path)
    assert reloaded.engaged is True
    assert reloaded.reason == "CAPITAL SAFETY VIOLATION"
    assert reloaded.incident == {"type": "CAPITAL SAFETY VIOLATION", "scan": 12}


def test_a_restart_after_engagement_still_sees_it_engaged(tmp_path):
    """The exact scenario item 13 exists to prevent: engage, simulate a
    restart (nothing but a fresh load_state call, no in-memory state
    carried over), confirm the fresh process still sees engaged=True."""
    path = tmp_path / "ks.json"
    save_state(engage(reason="UNHEDGED POSITION", at="t0", incident={}), path)
    fresh_process_view = load_state(path)  # a brand new call, no shared state with the line above
    assert fresh_process_view.engaged is True


def test_clear_kill_switch_removes_the_file_and_restores_not_engaged(tmp_path):
    path = tmp_path / "ks.json"
    save_state(engage(reason="x", at="t0", incident={}), path)
    assert load_state(path).engaged is True
    clear_kill_switch(path)
    assert load_state(path).engaged is False


def test_clear_kill_switch_on_a_nonexistent_file_is_a_noop(tmp_path):
    path = tmp_path / "does_not_exist.json"
    clear_kill_switch(path)  # must not raise
    assert load_state(path).engaged is False


def test_malformed_file_is_treated_as_engaged_never_as_clear(tmp_path):
    path = tmp_path / "ks.json"
    path.write_text("{not valid json")
    state = load_state(path)
    assert state.engaged is True
    assert "could not be read" in (state.reason or "")


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "ks.json"
    save_state(KillSwitchState(engaged=False), path)
    assert path.exists()
    assert json.loads(path.read_text())["engaged"] is False
