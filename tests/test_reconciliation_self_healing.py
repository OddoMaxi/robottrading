from app.execution.reconciliation import reconcile_base_asset_balance
from app.operations.reconciliation_self_healing import (
    CandidateExplanationEvent,
    attempt_reconciliation_recovery,
)


def test_already_matching_result_needs_no_healing():
    original = reconcile_base_asset_balance(exchange="binance", before_balance=10.0, after_balance=10.0)
    result = attempt_reconciliation_recovery(original_result=original, candidate_events=())
    assert result.recovered is True
    assert result.explaining_events == ()
    assert result.final_result is original


def test_rvn_incident_is_auto_recovered_by_the_real_rebalance_sell_event():
    """Even independent of the targeted FIX 3 to
    reconcile_base_asset_balance itself, self-healing must be able to
    discover the exact real explanation (the 2192.5 RVN rebalance sell,
    confirmed against real Binance myTrades order 1344692249) given only
    the ORIGINAL (pre-FIX-3-shaped) reconciliation call that didn't know
    about it."""
    original = reconcile_base_asset_balance(
        exchange="binance", before_balance=14400.3175, after_balance=14332.9902,
        arbitrage_buy_exchange="binance", arbitrage_buy_net_qty=2125.1727,
    )
    assert original.match is False  # the real incident, reproduced

    candidates = (CandidateExplanationEvent(label="rebalance_sell(binance, 2192.5 RVN, order 1344692249)", signed_qty=-2192.5),)
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)

    assert healed.recovered is True
    assert healed.final_result.match is True
    assert "1344692249" in healed.diagnostic
    assert len(healed.explaining_events) == 1


def test_smallest_explaining_subset_is_preferred():
    original = reconcile_base_asset_balance(exchange="binance", before_balance=100.0, after_balance=70.0)
    candidates = (
        CandidateExplanationEvent(label="single_event_explains_it", signed_qty=-30.0),
        CandidateExplanationEvent(label="irrelevant_a", signed_qty=5.0),
        CandidateExplanationEvent(label="irrelevant_b", signed_qty=-5.0),
    )
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)
    assert healed.recovered is True
    assert len(healed.explaining_events) == 1
    assert healed.explaining_events[0].label == "single_event_explains_it"


def test_unexplainable_mismatch_is_a_genuine_safe_stop():
    original = reconcile_base_asset_balance(exchange="binance", before_balance=100.0, after_balance=1.0)
    candidates = (
        CandidateExplanationEvent(label="too_small_a", signed_qty=-2.0),
        CandidateExplanationEvent(label="too_small_b", signed_qty=-1.0),
    )
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)
    assert healed.recovered is False
    assert healed.final_result.match is False
    assert "SAFE STOP" in healed.diagnostic
    assert healed.explaining_events == ()


def test_no_candidate_events_and_a_real_mismatch_is_a_safe_stop():
    original = reconcile_base_asset_balance(exchange="binance", before_balance=100.0, after_balance=1.0)
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=())
    assert healed.recovered is False


def test_never_loosens_the_original_tolerance():
    """A combination that overshoots past the original tolerance must
    not be accepted just because it's closer than doing nothing."""
    original = reconcile_base_asset_balance(exchange="binance", before_balance=100.0, after_balance=100.0, tolerance_abs=0.01)
    candidates = (CandidateExplanationEvent(label="too_big", signed_qty=5.0),)
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)
    assert healed.recovered is True  # original already matched (delta=0 within tolerance)
    assert healed.explaining_events == ()  # and the oversized candidate must not be spuriously applied
