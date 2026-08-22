"""PRE-PHASE-2 CORRECTIVE MAINTENANCE #2, fix 1 — economic-event
deduplication. Direct regression for the confirmed bug: MASTER
previously allocated capital and credited P&L to BOTH the atomic and
sequential twin of the SAME real-world price gap (0% OLD-vs-MASTER
agreement on `atomic` in the first Shadow Mode validation)."""

import uuid
from datetime import UTC, datetime

from app.shadow.dedup import economic_event_key, partition_duplicate_economic_events
from app.shadow.models import Engine, ShadowOpportunitySummary

SAME_LEGS = [
    {"chain": "solana", "exchange": "raydium", "pool_id": "pool_a", "price": 100.0},
    {"chain": "solana", "exchange": "orca", "pool_id": "pool_b", "price": 101.0},
]


def _opp(**overrides) -> ShadowOpportunitySummary:
    defaults = dict(
        opportunity_id=uuid.uuid4(),
        engine=Engine.DEX,
        strategy="dex_triangular",
        symbol="SOL/USDC",
        legs=SAME_LEGS,
        chain="solana",
        expected_profit_usd=5.0,
        capital_usd=1_000.0,
        execution_fill_probability=1.0,
        capital_velocity_score=50.0,
        holding_period_seconds=9.0,
        detected_at=datetime(2026, 8, 22, 12, 0, 0),
        detection_time_rejection_reason=None,
    )
    defaults.update(overrides)
    return ShadowOpportunitySummary(**defaults)


def test_economic_event_key_identical_for_atomic_and_sequential_twin():
    sequential = _opp(strategy="dex_triangular")
    atomic = _opp(strategy="atomic")  # same detected_at, same legs, different strategy/id
    assert economic_event_key(sequential) == economic_event_key(atomic)


def test_economic_event_key_differs_when_legs_differ():
    a = _opp(legs=SAME_LEGS)
    b = _opp(legs=[{"chain": "eth", "exchange": "uniswap_v3"}])
    assert economic_event_key(a) != economic_event_key(b)


def test_economic_event_key_differs_when_detected_at_differs():
    a = _opp(detected_at=datetime(2026, 8, 22, 12, 0, 0))
    b = _opp(detected_at=datetime(2026, 8, 22, 12, 0, 1))
    assert economic_event_key(a) != economic_event_key(b)


def test_partition_singleton_opportunity_is_its_own_representative():
    opp = _opp()
    representatives, duplicates = partition_duplicate_economic_events([opp])
    assert representatives == [opp]
    assert duplicates == {}


def test_partition_atomic_sequential_pair_keeps_only_the_higher_ranked_one():
    """The actual regression: given a real atomic/sequential pair (same
    legs+detected_at), exactly ONE survives as a representative — the
    other is flagged as a duplicate loser, never independently allocated."""
    sequential = _opp(strategy="dex_triangular", capital_velocity_score=70.0)
    atomic = _opp(strategy="atomic", capital_velocity_score=40.0)  # lower rank -> loses

    representatives, duplicates = partition_duplicate_economic_events([sequential, atomic])

    assert len(representatives) == 1
    assert representatives[0].opportunity_id == sequential.opportunity_id
    assert duplicates == {atomic.opportunity_id: sequential}


def test_partition_picks_whichever_side_has_the_higher_rank_score():
    """Not always "sequential wins" — MASTER decides independently by
    rank score, unlike blindly trusting OLD's own duplicate marking."""
    sequential = _opp(strategy="dex_triangular", capital_velocity_score=10.0)
    atomic = _opp(strategy="atomic", capital_velocity_score=90.0)  # higher rank -> wins

    representatives, duplicates = partition_duplicate_economic_events([sequential, atomic])

    assert representatives[0].opportunity_id == atomic.opportunity_id
    assert duplicates == {sequential.opportunity_id: atomic}


def test_partition_never_produces_more_than_one_winner_per_economic_event():
    """A 3-way group (e.g. atomic + two differently-priced sequential
    candidates sharing the same legs/instant, an observed real pattern)
    must still collapse to exactly one representative."""
    a = _opp(strategy="dex_triangular", capital_velocity_score=10.0)
    b = _opp(strategy="atomic", capital_velocity_score=20.0)
    c = _opp(strategy="dex_multihop", capital_velocity_score=15.0)

    representatives, duplicates = partition_duplicate_economic_events([a, b, c])

    assert len(representatives) == 1
    assert representatives[0].opportunity_id == b.opportunity_id
    assert set(duplicates.keys()) == {a.opportunity_id, c.opportunity_id}


def test_partition_unrelated_opportunities_are_never_grouped_together():
    a = _opp(detected_at=datetime(2026, 8, 22, 12, 0, 0))
    b = _opp(detected_at=datetime(2026, 8, 22, 12, 5, 0))  # different instant, same legs -> different event
    representatives, duplicates = partition_duplicate_economic_events([a, b])
    assert len(representatives) == 2
    assert duplicates == {}


def test_partition_opportunities_with_no_legs_are_never_grouped():
    a = _opp(legs=[])
    b = _opp(legs=[])
    representatives, duplicates = partition_duplicate_economic_events([a, b])
    assert len(representatives) == 2  # each is its own singleton event, never falsely merged
    assert duplicates == {}
