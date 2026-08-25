import json
from pathlib import Path

import pytest

from app.operations.incident_knowledge_base import (
    SEED_KNOWN_INCIDENTS,
    KnownIncident,
    add_resolved_incident,
    lookup_known_incident,
    load_state,
    record_occurrence,
    save_state,
)

NONEXISTENT = Path("/nonexistent/kb.json")


def test_seed_has_no_duplicate_signatures():
    sigs = [k.incident_signature for k in SEED_KNOWN_INCIDENTS]
    assert len(sigs) == len(set(sigs))


def test_seed_entries_all_have_non_empty_fields():
    for k in SEED_KNOWN_INCIDENTS:
        assert k.incident_signature and k.root_cause and k.safe_recovery and k.validation
        assert k.first_seen and k.last_seen
        assert k.occurrence_count >= 1


def test_load_state_without_a_file_returns_the_seed(tmp_path):
    state = load_state(tmp_path / "does_not_exist.json")
    assert len(state) == len(SEED_KNOWN_INCIDENTS)
    assert "BYBIT_ORDER_LINK_ID_TOO_LONG" in state


def test_lookup_known_incident_finds_a_seeded_entry():
    state = load_state(NONEXISTENT)
    result = lookup_known_incident(state, "RECONCILIATION_MISSING_REBALANCE_EVENT")
    assert result is not None
    assert "rebalance" in result.root_cause.lower()


def test_lookup_unknown_signature_returns_none():
    state = load_state(NONEXISTENT)
    assert lookup_known_incident(state, "SOMETHING_NEVER_SEEN") is None


def test_record_occurrence_bumps_count_and_last_seen_without_mutating_input():
    state = load_state(NONEXISTENT)
    original = state["BYBIT_ORDER_LINK_ID_TOO_LONG"]
    new_state = record_occurrence(state, "BYBIT_ORDER_LINK_ID_TOO_LONG", at="2026-09-01")
    assert state["BYBIT_ORDER_LINK_ID_TOO_LONG"] is original  # input untouched
    assert new_state["BYBIT_ORDER_LINK_ID_TOO_LONG"].occurrence_count == original.occurrence_count + 1
    assert new_state["BYBIT_ORDER_LINK_ID_TOO_LONG"].last_seen == "2026-09-01"


def test_record_occurrence_of_unknown_signature_raises():
    state = load_state(NONEXISTENT)
    with pytest.raises(KeyError):
        record_occurrence(state, "NEVER_SEEDED", at="2026-09-01")


def test_add_resolved_incident_adds_a_new_entry():
    state = load_state(NONEXISTENT)
    new_incident = KnownIncident(
        incident_signature="BRAND_NEW_THING", root_cause="x", safe_recovery="y", validation="z",
        first_seen="2026-09-01", last_seen="2026-09-01", occurrence_count=1,
    )
    new_state = add_resolved_incident(state, new_incident)
    assert "BRAND_NEW_THING" not in state  # input untouched
    assert new_state["BRAND_NEW_THING"] == new_incident


def test_save_then_load_round_trips(tmp_path):
    path = tmp_path / "kb.json"
    state = load_state(NONEXISTENT)
    state = record_occurrence(state, "BYBIT_ORDER_LINK_ID_TOO_LONG", at="2026-09-01")
    save_state(state, path)
    reloaded = load_state(path)
    assert reloaded["BYBIT_ORDER_LINK_ID_TOO_LONG"].occurrence_count == 2
    assert reloaded["BYBIT_ORDER_LINK_ID_TOO_LONG"].last_seen == "2026-09-01"
    assert len(reloaded) == len(state)


def test_save_creates_parent_directories(tmp_path):
    path = tmp_path / "nested" / "dir" / "kb.json"
    save_state({}, path)
    assert path.exists()
    assert json.loads(path.read_text()) == {}
