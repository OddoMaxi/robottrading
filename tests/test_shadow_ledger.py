import uuid

from app.shadow.ledger import ShadowCapitalLedger


def test_shadow_ledger_starts_fully_available():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    assert ledger.available_capital_usd(now=0.0) == 10_000.0


def test_shadow_ledger_reserve_locks_capital_until_release():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    rid = uuid.uuid4()
    assert ledger.reserve(rid, 4_000.0, now=0.0, release_at=10.0) is True
    assert ledger.available_capital_usd(now=5.0) == 6_000.0
    assert ledger.available_capital_usd(now=10.0) == 10_000.0  # freed after the window


def test_shadow_ledger_never_over_reserves():
    ledger = ShadowCapitalLedger(total_capital_usd=1_000.0)
    assert ledger.reserve(uuid.uuid4(), 1_000.0, now=0.0, release_at=10.0) is True
    assert ledger.reserve(uuid.uuid4(), 0.01, now=0.0, release_at=10.0) is False


def test_shadow_ledger_two_opportunities_in_the_same_instant_genuinely_compete():
    """Direct proof the shadow allocator has the SAME concurrency-safety
    property the Reality Audit fixed in the real DexCapitalPool — capital
    reserved in one instant is unavailable to a second request in the
    same instant, not synchronously "free again" the moment the first
    resolves."""
    ledger = ShadowCapitalLedger(total_capital_usd=5_000.0)
    same_now = 100.0
    assert ledger.reserve(uuid.uuid4(), 5_000.0, now=same_now, release_at=same_now + 20.0) is True
    assert ledger.available_capital_usd(now=same_now) == 0.0
    assert ledger.reserve(uuid.uuid4(), 5_000.0, now=same_now, release_at=same_now + 20.0) is False


def test_shadow_ledger_resolve_pnl_books_immediately_but_principal_stays_locked():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    ledger.reserve(uuid.uuid4(), 1_000.0, now=0.0, release_at=10.0)
    ledger.resolve_pnl(50.0)
    assert ledger.available_capital_usd(now=0.0) == 9_050.0  # 10,000 + 50 profit - 1,000 still locked
    assert ledger.available_capital_usd(now=10.0) == 10_050.0
