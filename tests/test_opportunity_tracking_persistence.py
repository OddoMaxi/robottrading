"""Regression tests for the PRE-PHASE-2 CORRECTIVE MAINTENANCE fix
(2026-08-22): update_opportunity_tracking used to be called by main.py's
DEX continuation path with rejection_reason=None hardcoded, silently
erasing the duplicate_economic_event marking set earlier the same cycle.
Fixed by passing the opportunity's own freshly-recomputed
rejection_reason — matching the CEX continuation call site's already-
correct pattern. These tests exercise app.database.repository.
update_opportunity_tracking directly (no live DB needed — a fake session
just captures the compiled UPDATE statement's bound parameters), proving
the function itself faithfully persists whatever rejection_reason is
passed rather than ever substituting a hardcoded value.
"""

import uuid

import pytest

from app.config.constants import OpportunityStatus, Strategy
from app.database.repository import update_opportunity_tracking
from app.opportunity.models import Opportunity
from app.opportunity.tracker import TrackedOpportunity


class _CapturingSession:
    def __init__(self):
        self.captured_statements: list = []

    async def execute(self, stmt):
        self.captured_statements.append(stmt)
        return None


def _tracked(**overrides) -> TrackedOpportunity:
    defaults = dict(
        key="dex_triangular|X/Y|solana_raydium",
        opportunity_id=uuid.uuid4(),
        first_seen=0.0,
        last_seen=10.0,
        initial_edge_pct=1.0,
        current_edge_pct=1.0,
        max_edge_pct=1.0,
        min_edge_pct=1.0,
        updates_count=2,
        status=OpportunityStatus.DETECTED,
    )
    defaults.update(overrides)
    return TrackedOpportunity(**defaults)


def _opportunity(**overrides) -> Opportunity:
    defaults = dict(strategy=Strategy.DEX_TRIANGULAR, symbol="X/Y", legs=[], gross_spread_pct=1.0, id=uuid.uuid4())
    defaults.update(overrides)
    return Opportunity(**defaults)


def _bound_params(session: _CapturingSession) -> dict:
    return session.captured_statements[-1].compile().params


@pytest.mark.asyncio
async def test_continuation_update_preserves_duplicate_marking_when_caller_passes_it():
    """The actual regression: a continuation of an opportunity ALREADY
    marked duplicate_economic_event must still be marked
    duplicate_economic_event after the update — not erased to None."""
    session = _CapturingSession()
    opp = _opportunity(rejection_reason="duplicate_economic_event")

    await update_opportunity_tracking(session, _tracked(), opp, rejection_reason=opp.rejection_reason)

    assert _bound_params(session)["rejection_reason"] == "duplicate_economic_event"


@pytest.mark.asyncio
async def test_continuation_update_survives_repeated_scans():
    """Simulates several consecutive continuation scans of the SAME
    persisting duplicate opportunity (exactly what main.py's DEX
    detection loop does every ~53s poll cycle) — the marking must not
    degrade or flip to None on any of them."""
    session = _CapturingSession()
    opp = _opportunity(rejection_reason="duplicate_economic_event")

    for _ in range(5):
        await update_opportunity_tracking(session, _tracked(), opp, rejection_reason=opp.rejection_reason)
        assert _bound_params(session)["rejection_reason"] == "duplicate_economic_event"


@pytest.mark.asyncio
async def test_continuation_update_can_still_genuinely_clear_a_rejection_reason():
    """Not "always preserve no matter what" — a caller that legitimately
    re-evaluates and finds the opportunity no longer rejected (e.g. the
    CEX validate() gate approving something it previously rejected) must
    still be able to clear it. The fix is "don't silently overwrite with
    a hardcoded value," not "never allow a real change.\""""
    session = _CapturingSession()
    opp = _opportunity(rejection_reason=None)

    await update_opportunity_tracking(session, _tracked(), opp, rejection_reason=opp.rejection_reason)

    assert _bound_params(session)["rejection_reason"] is None


@pytest.mark.asyncio
async def test_continuation_update_refreshes_other_snapshot_fields_from_the_opportunity_unaffected_by_the_fix():
    """Confirms the fix didn't accidentally change behavior for every
    OTHER field, which were never part of the bug — they're already
    sourced directly from the fresh `opportunity` object, not a
    separately-erasable parameter."""
    session = _CapturingSession()
    opp = _opportunity(net_spread_pct=1.23, capital_usd=456.0, expected_profit_usd=5.0)

    await update_opportunity_tracking(session, _tracked(), opp, rejection_reason=None)

    params = _bound_params(session)
    assert params["net_spread_pct"] == 1.23
    assert params["capital_usd"] == 456.0
    assert params["expected_profit_usd"] == 5.0
