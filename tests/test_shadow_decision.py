import uuid
from datetime import UTC, datetime

from app.shadow.decision import decisions_agree, duplicate_economic_event_decision, evaluate_shadow_decision
from app.shadow.ledger import ShadowCapitalLedger
from app.shadow.models import Engine, MasterOutcome, ShadowOpportunitySummary
from app.shadow.positions import ShadowOpenPositionTracker


def _opp(**overrides) -> ShadowOpportunitySummary:
    defaults = dict(
        opportunity_id=uuid.uuid4(),
        engine=Engine.CEX,
        strategy="cross_exchange",
        symbol="BTC/USDT",
        legs=[{"exchange": "binance"}, {"exchange": "bybit"}],
        chain=None,
        expected_profit_usd=5.0,
        capital_usd=1_000.0,
        execution_fill_probability=1.0,
        capital_velocity_score=50.0,
        holding_period_seconds=30.0,
        detected_at=datetime.now(UTC).replace(tzinfo=None),
        detection_time_rejection_reason=None,
    )
    defaults.update(overrides)
    return ShadowOpportunitySummary(**defaults)


def test_evaluate_shadow_decision_allocates_when_capital_and_ev_are_fine():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    decision = evaluate_shadow_decision(_opp(), ledger, tracker, now)
    assert decision.outcome == MasterOutcome.ALLOCATE
    assert decision.theoretical_capital_reserved_usd == 1_000.0
    assert decision.projected_net_profit_usd == 5.0
    assert ledger.available_capital_usd(now.timestamp()) == 10_000.0 - 1_000.0 + 5.0


def test_evaluate_shadow_decision_rejects_negative_ev():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    decision = evaluate_shadow_decision(_opp(expected_profit_usd=-2.0), ledger, tracker, now)
    assert decision.outcome == MasterOutcome.REJECT_NOT_PROFITABLE
    assert decision.theoretical_capital_reserved_usd is None


def test_evaluate_shadow_decision_rejects_missing_data():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    decision = evaluate_shadow_decision(_opp(expected_profit_usd=None, capital_velocity_score=None), ledger, tracker, now)
    assert decision.outcome == MasterOutcome.REJECT_MISSING_DATA


def test_evaluate_shadow_decision_sizes_down_to_whatever_capital_is_available():
    """Matches the already-established behavior of the real engines
    (app.onchain.dex_paper_trader.attempt_dex_trade,
    app.simulation.paper_trader.simulate): requesting more than the
    total pool clips to what's actually available, it doesn't reject
    outright — a smaller, real allocation beats no allocation at all."""
    ledger = ShadowCapitalLedger(total_capital_usd=500.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    decision = evaluate_shadow_decision(_opp(capital_usd=1_000.0, expected_profit_usd=5.0), ledger, tracker, now)
    assert decision.outcome == MasterOutcome.ALLOCATE
    assert decision.theoretical_capital_reserved_usd == 500.0


def test_evaluate_shadow_decision_rejects_when_pool_is_already_fully_locked():
    ledger = ShadowCapitalLedger(total_capital_usd=500.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    ledger.reserve(uuid.uuid4(), 500.0, now=now.timestamp(), release_at=now.timestamp() + 60.0)
    decision = evaluate_shadow_decision(_opp(capital_usd=1_000.0), ledger, tracker, now)
    assert decision.outcome == MasterOutcome.REJECT_NO_CAPITAL


def test_evaluate_shadow_decision_two_opportunities_same_instant_genuinely_compete():
    """The exact cross-engine capital-conflict scenario Phase 2 exists to
    detect: a CEX and a DEX opportunity, both wanting the full shadow
    pool at the same instant — only one can be allocated. Different
    strategy/symbol/legs so this test isolates CAPITAL contention, not
    position-already-open (see test_shadow_positions.py for that)."""
    ledger = ShadowCapitalLedger(total_capital_usd=5_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    cex_opp = _opp(engine=Engine.CEX, strategy="cross_exchange", symbol="BTC/USDT", capital_usd=5_000.0, expected_profit_usd=10.0)
    dex_opp = _opp(
        engine=Engine.DEX, strategy="dex_triangular", symbol="SOL/USDC", legs=[{"chain": "solana", "exchange": "raydium"}],
        capital_usd=5_000.0, expected_profit_usd=10.0,
    )

    first = evaluate_shadow_decision(cex_opp, ledger, tracker, now)
    second = evaluate_shadow_decision(dex_opp, ledger, tracker, now)

    assert first.outcome == MasterOutcome.ALLOCATE
    assert first.theoretical_capital_reserved_usd == 5_000.0
    # The second request, in the SAME instant, must NOT see the full pool
    # again — only the sliver of profit already compounded in from the
    # first allocation (same invariant proven for the real DexCapitalPool
    # during the Reality Audit).
    assert second.theoretical_capital_reserved_usd is None or second.theoretical_capital_reserved_usd < 5_000.0


def test_duplicate_economic_event_decision_never_reserves_capital():
    decision = duplicate_economic_event_decision(rank_score=42.0, winner_opportunity_id=uuid.uuid4(), capital_available_global_usd=1000.0)
    assert decision.outcome == MasterOutcome.REJECT_DUPLICATE_ECONOMIC_EVENT
    assert decision.theoretical_capital_reserved_usd is None
    assert decision.projected_net_profit_usd is None


def test_decisions_agree_matches_boolean_approval():
    assert decisions_agree(old_approved=True, master_approved=True) is True
    assert decisions_agree(old_approved=False, master_approved=False) is True
    assert decisions_agree(old_approved=True, master_approved=False) is False
    assert decisions_agree(old_approved=False, master_approved=True) is False
