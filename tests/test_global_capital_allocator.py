import uuid

from app.orchestration.global_allocator import GlobalCapitalAllocator, try_reserve_for_opportunity


def test_allocator_starts_fully_available_and_reconciled():
    allocator = GlobalCapitalAllocator(total_capital_usd=10_000.0)
    assert allocator.available_capital_usd(now=0.0) == 10_000.0
    assert allocator.check_invariant(now=0.0) == []


def test_allocator_reserve_locks_capital_until_release():
    allocator = GlobalCapitalAllocator(total_capital_usd=10_000.0)
    rid = uuid.uuid4()
    assert allocator.reserve(rid, 4_000.0, now=0.0, release_at=10.0, engine="CEX") is True
    assert allocator.available_capital_usd(now=5.0) == 6_000.0
    assert allocator.available_capital_usd(now=10.0) == 10_000.0
    assert allocator.check_invariant(now=5.0) == []


def test_allocator_never_over_reserves():
    allocator = GlobalCapitalAllocator(total_capital_usd=1_000.0)
    assert allocator.reserve(uuid.uuid4(), 1_000.0, now=0.0, release_at=10.0, engine="CEX") is True
    assert allocator.reserve(uuid.uuid4(), 0.01, now=0.0, release_at=10.0, engine="DEX") is False
    assert allocator.available_capital_usd(now=0.0) == 0.0


def test_allocator_atomic_cross_engine_reservation_same_instant():
    """The exact scenario Phase 2C exists to prevent: a CEX opportunity
    and a DEX opportunity, both wanting the full pool at the same
    instant — the same simulated dollar can never be reserved by both."""
    allocator = GlobalCapitalAllocator(total_capital_usd=5_000.0)
    same_now = 100.0
    cex_rid, dex_rid = uuid.uuid4(), uuid.uuid4()
    assert allocator.reserve(cex_rid, 5_000.0, now=same_now, release_at=same_now + 20.0, engine="CEX") is True
    assert allocator.available_capital_usd(now=same_now) == 0.0
    assert allocator.reserve(dex_rid, 5_000.0, now=same_now, release_at=same_now + 20.0, engine="DEX") is False
    assert allocator.check_invariant(now=same_now) == []


def test_allocator_adjust_reservation_frees_unused_excess_immediately():
    allocator = GlobalCapitalAllocator(total_capital_usd=1_000.0)
    rid = uuid.uuid4()
    allocator.reserve(rid, 800.0, now=0.0, release_at=60.0, engine="CEX")
    assert allocator.available_capital_usd(now=1.0) == 200.0
    allocator.adjust_reservation(rid, actual_amount=300.0)  # executor only actually used $300
    assert allocator.available_capital_usd(now=1.0) == 700.0
    assert allocator.check_invariant(now=1.0) == []


def test_allocator_adjust_reservation_ignores_an_attempted_increase():
    """adjust_reservation is documented as a one-way SHRINK — the
    executor's own capital check can only be MORE restrictive than
    what MASTER granted, never less, so an "increase" would indicate a
    bug elsewhere and must not silently inflate the lock."""
    allocator = GlobalCapitalAllocator(total_capital_usd=1_000.0)
    rid = uuid.uuid4()
    allocator.reserve(rid, 300.0, now=0.0, release_at=60.0, engine="CEX")
    allocator.adjust_reservation(rid, actual_amount=900.0)
    assert allocator.locked_capital_usd(now=1.0) == 300.0


def test_allocator_release_reservation_frees_capital_immediately():
    allocator = GlobalCapitalAllocator(total_capital_usd=1_000.0)
    rid = uuid.uuid4()
    allocator.reserve(rid, 500.0, now=0.0, release_at=60.0, engine="DEX")
    released = allocator.release_reservation(rid)
    assert released == 500.0
    assert allocator.available_capital_usd(now=0.0) == 1_000.0


def test_allocator_resolve_pnl_books_immediately_principal_stays_locked():
    allocator = GlobalCapitalAllocator(total_capital_usd=10_000.0)
    rid = uuid.uuid4()
    allocator.reserve(rid, 1_000.0, now=0.0, release_at=10.0, engine="CEX")
    allocator.resolve_pnl(25.0)
    assert allocator.available_capital_usd(now=0.0) == 9_025.0  # 10,000 + 25 profit - 1,000 still locked
    assert allocator.available_capital_usd(now=10.0) == 10_025.0
    assert allocator.check_invariant(now=10.0) == []


def test_allocator_invariant_detects_negative_available_if_state_were_corrupted():
    """Direct-construct a broken state (bypassing reserve()'s own guard)
    to prove check_invariant actually catches it — the guard that would
    normally prevent this in practice is a SEPARATE line of defense."""
    from app.orchestration.global_allocator import _Reservation

    allocator = GlobalCapitalAllocator(total_capital_usd=100.0)
    allocator._reservations[uuid.uuid4()] = _Reservation(amount=500.0, release_at=60.0, engine="CEX")
    violations = allocator.check_invariant(now=0.0)
    assert violations
    assert any("negative" in v for v in violations)


def test_try_reserve_for_opportunity_sizes_down_to_available():
    allocator = GlobalCapitalAllocator(total_capital_usd=500.0)
    grant = try_reserve_for_opportunity(allocator, uuid.uuid4(), capital_requested_usd=1_000.0, holding_period_seconds=30.0, now=0.0, engine="CEX")
    assert grant is not None
    assert grant.amount == 500.0


def test_try_reserve_for_opportunity_none_when_nothing_available():
    allocator = GlobalCapitalAllocator(total_capital_usd=100.0)
    allocator.reserve(uuid.uuid4(), 100.0, now=0.0, release_at=60.0, engine="DEX")
    grant = try_reserve_for_opportunity(allocator, uuid.uuid4(), capital_requested_usd=50.0, holding_period_seconds=30.0, now=0.0, engine="CEX")
    assert grant is None


def test_allocator_session_counters_start_at_zero_and_increment():
    allocator = GlobalCapitalAllocator()
    assert allocator.grants_count == 0
    assert allocator.rejections_count == 0
    assert allocator.fills_count == 0
    allocator.record_grant()
    allocator.record_grant()
    allocator.record_rejection()
    allocator.record_fill()
    assert allocator.grants_count == 2
    assert allocator.rejections_count == 1
    assert allocator.fills_count == 1


def test_try_reserve_for_opportunity_none_when_no_capital_requested():
    allocator = GlobalCapitalAllocator(total_capital_usd=1_000.0)
    assert try_reserve_for_opportunity(allocator, uuid.uuid4(), capital_requested_usd=None, holding_period_seconds=30.0, now=0.0, engine="CEX") is None
    assert try_reserve_for_opportunity(allocator, uuid.uuid4(), capital_requested_usd=0.0, holding_period_seconds=30.0, now=0.0, engine="CEX") is None
