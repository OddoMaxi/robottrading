"""Phase 2D, item 1 (user directive, 2026-08-23) — rollback observability.

Proves manual AND automatic rollbacks are persisted to system_events with
timestamp, reason, previous/new state, and an explicit origin
("manual"/"automatic") — closing the gap disclosed at the end of Phase 2C
(manual rollback via /master/rollback never called log_system_event).

No real database is used (this repo has no DB test fixtures — models use
postgresql-only column types). Instead, a FakeSession captures exactly what
would have been persisted via session.add(), the same technique used to
prove capital-safety guarantees in test_phase2c_isolation.py.
"""

import uuid

import pytest

import main
from app.api.routes import MasterRollbackRequest, master_enable, master_rollback
from app.orchestration.control import MasterPaperControl
from app.orchestration.global_allocator import GlobalCapitalAllocator, _Reservation


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass

    async def commit(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _isolated_master_control(monkeypatch):
    """Every test gets its own MasterPaperControl so state never leaks
    into the shared singleton other tests/modules rely on."""
    control = MasterPaperControl()
    monkeypatch.setattr("app.api.routes.master_control", control)
    monkeypatch.setattr("main.master_control", control)
    return control


async def test_manual_rollback_via_endpoint_is_persisted_with_origin_manual():
    session = FakeSession()
    await master_rollback(MasterRollbackRequest(reason="operator judgment call"), session=session)

    assert len(session.added) == 1
    event = session.added[0]
    assert event.event_type == "master_rollback"
    assert event.message == "operator judgment call"
    assert event.event_metadata["origin"] == "manual"
    assert event.event_metadata["previous_state"] == {"paper_authority_enabled": True}
    assert event.event_metadata["new_state"]["paper_authority_enabled"] is False
    assert event.event_metadata["new_state"]["rollback_reason"] == "operator judgment call"
    assert event.event_metadata["new_state"]["rollback_at"] is not None


async def test_manual_enable_via_endpoint_is_persisted(_isolated_master_control):
    _isolated_master_control.disable("prior rollback", now=1.0)
    session = FakeSession()
    await master_enable(session=session)

    assert len(session.added) == 1
    event = session.added[0]
    assert event.event_type == "master_enable"
    assert event.event_metadata["origin"] == "manual"
    assert event.event_metadata["previous_state"]["paper_authority_enabled"] is False
    assert event.event_metadata["new_state"]["paper_authority_enabled"] is True


async def test_automatic_rollback_is_persisted_with_origin_automatic(monkeypatch):
    """Injects an invariant-breaking reservation directly (same technique
    as test_global_capital_allocator's own invariant test) to force the
    automatic rollback path in main._master_check_invariant_and_maybe_rollback."""
    allocator = GlobalCapitalAllocator(total_capital_usd=100.0)
    allocator._reservations[uuid.uuid4()] = _Reservation(amount=500.0, release_at=60.0, engine="CEX")
    monkeypatch.setattr("main.global_allocator", allocator)

    session = FakeSession()
    await main._master_check_invariant_and_maybe_rollback(session, now=0.0)

    assert main.master_control.paper_authority_enabled is False
    assert len(session.added) == 1
    event = session.added[0]
    assert event.event_type == "master_rollback"
    assert event.event_metadata["origin"] == "automatic"
    assert event.event_metadata["previous_state"] == {"paper_authority_enabled": True}
    assert event.event_metadata["new_state"]["paper_authority_enabled"] is False


async def test_automatic_rollback_does_not_fire_again_once_already_disabled(monkeypatch):
    """Second invariant check after rollback must be a no-op — otherwise
    every subsequent scan would log a duplicate rollback event forever."""
    allocator = GlobalCapitalAllocator(total_capital_usd=100.0)
    allocator._reservations[uuid.uuid4()] = _Reservation(amount=500.0, release_at=60.0, engine="CEX")
    monkeypatch.setattr("main.global_allocator", allocator)

    session = FakeSession()
    await main._master_check_invariant_and_maybe_rollback(session, now=0.0)
    await main._master_check_invariant_and_maybe_rollback(session, now=1.0)

    assert len(session.added) == 1
