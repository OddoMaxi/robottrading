"""PRE-PHASE-2 CORRECTIVE MAINTENANCE #2, fix 2 — position_already_open
reproduction for CEX / hold-based strategies. Direct regression for the
confirmed bug: MASTER previously "allocated" to the same persisting
cross_exchange spread dozens of times per hour with no concept of an
already-open position (6.1% OLD-vs-MASTER agreement on cross_exchange in
the first Shadow Mode validation)."""

import uuid
from datetime import UTC, datetime

from app.shadow.decision import evaluate_shadow_decision
from app.shadow.ledger import ShadowCapitalLedger
from app.shadow.models import Engine, MasterOutcome, ShadowOpportunitySummary
from app.shadow.positions import DEFAULT_MIN_REENTRY_DELAY_SECONDS, ShadowOpenPositionTracker, position_key_for


def _opp(**overrides) -> ShadowOpportunitySummary:
    defaults = dict(
        opportunity_id=uuid.uuid4(),
        engine=Engine.CEX,
        strategy="cross_exchange",
        symbol="LUNC/USDT",
        legs=[{"exchange": "binance"}, {"exchange": "bybit"}],
        chain=None,
        expected_profit_usd=1.0,
        capital_usd=1_000.0,
        execution_fill_probability=1.0,
        capital_velocity_score=50.0,
        holding_period_seconds=8.0,
        detected_at=datetime.now(UTC).replace(tzinfo=None),
        detection_time_rejection_reason=None,
    )
    defaults.update(overrides)
    return ShadowOpportunitySummary(**defaults)


def test_position_key_for_matches_validators_own_derivation():
    """Same shape app.execution.validator.validate() uses:
    (strategy, legs[0].exchange, symbol) — copied exactly, not
    reinvented, so MASTER blocks on the SAME key OLD does."""
    key = position_key_for("cross_exchange", [{"exchange": "binance"}, {"exchange": "bybit"}], "LUNC/USDT")
    assert key == ("cross_exchange", "binance", "LUNC/USDT")


def test_position_key_for_none_when_no_legs():
    assert position_key_for("cross_exchange", [], "LUNC/USDT") is None


def test_shadow_open_position_tracker_blocks_reentry_while_open():
    tracker = ShadowOpenPositionTracker()
    key = ("cross_exchange", "binance", "LUNC/USDT")
    tracker.open_position(key, now=100.0, holding_period_seconds=8.0)
    assert tracker.is_open(key, now=100.0) is True
    assert tracker.is_open(key, now=107.9) is True


def test_shadow_open_position_tracker_frees_up_after_holding_period_plus_reentry_delay():
    tracker = ShadowOpenPositionTracker()
    key = ("cross_exchange", "binance", "LUNC/USDT")
    tracker.open_position(key, now=100.0, holding_period_seconds=8.0)
    expiry = 100.0 + 8.0 + DEFAULT_MIN_REENTRY_DELAY_SECONDS
    assert tracker.is_open(key, now=expiry - 0.01) is True
    assert tracker.is_open(key, now=expiry + 0.01) is False


def test_evaluate_shadow_decision_rejects_repeated_capture_of_a_persisting_spread():
    """The actual regression: the SAME cross_exchange opportunity
    (same key), re-detected on a later scan while its 8s hold hasn't
    elapsed yet, must be rejected — not independently re-allocated."""
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)

    first_capture = _opp()
    first_decision = evaluate_shadow_decision(first_capture, ledger, tracker, now)
    assert first_decision.outcome == MasterOutcome.ALLOCATE

    # Re-detected 2 seconds later — well within the still-open 8s hold.
    from datetime import timedelta

    second_capture = _opp(symbol=first_capture.symbol, strategy=first_capture.strategy, legs=first_capture.legs, detected_at=now + timedelta(seconds=2))
    second_decision = evaluate_shadow_decision(second_capture, ledger, tracker, now + timedelta(seconds=2))
    assert second_decision.outcome == MasterOutcome.REJECT_POSITION_ALREADY_OPEN


def test_evaluate_shadow_decision_allows_reentry_once_the_position_genuinely_closes():
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)

    first_capture = _opp(holding_period_seconds=8.0)
    evaluate_shadow_decision(first_capture, ledger, tracker, now)

    from datetime import timedelta

    much_later = now + timedelta(seconds=8.0 + DEFAULT_MIN_REENTRY_DELAY_SECONDS + 1.0)
    second_capture = _opp(symbol=first_capture.symbol, strategy=first_capture.strategy, legs=first_capture.legs, detected_at=much_later, holding_period_seconds=8.0)
    second_decision = evaluate_shadow_decision(second_capture, ledger, tracker, much_later)
    assert second_decision.outcome == MasterOutcome.ALLOCATE


def test_evaluate_shadow_decision_different_symbol_does_not_collide():
    """A different symbol is a genuinely different position key — must
    NOT be blocked by an unrelated open position."""
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)

    evaluate_shadow_decision(_opp(symbol="LUNC/USDT"), ledger, tracker, now)
    other_symbol_decision = evaluate_shadow_decision(_opp(symbol="BTC/USDT"), ledger, tracker, now)
    assert other_symbol_decision.outcome == MasterOutcome.ALLOCATE


def test_evaluate_shadow_decision_no_holding_period_skips_position_gating():
    """An opportunity with holding_period_seconds=None (shouldn't exist
    in practice for CEX, but defensively) must not crash the position
    check — it simply isn't gated by it."""
    ledger = ShadowCapitalLedger(total_capital_usd=10_000.0)
    tracker = ShadowOpenPositionTracker()
    now = datetime.now(UTC).replace(tzinfo=None)
    decision = evaluate_shadow_decision(_opp(holding_period_seconds=None), ledger, tracker, now)
    assert decision.outcome == MasterOutcome.ALLOCATE
