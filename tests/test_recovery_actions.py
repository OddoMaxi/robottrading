from app.operations.incident_knowledge_base import load_state
from app.operations.incident_taxonomy import IncidentCategory, RecoveryLevel, RecoveryScope
from app.operations.recovery_actions import (
    RecoveryActionType,
    classify_script_exception,
    decide_recovery_plan,
)
from pathlib import Path

KB = load_state(Path("/nonexistent/kb.json"))  # seed only


# ---- classify_script_exception: the item-2 pre/post-submission rule ------


def test_known_transient_exception_before_submission_is_retry_safe():
    c = classify_script_exception("ServerTimeoutError", order_submission_attempted=False)
    assert c.category == IncidentCategory.TRANSIENT
    assert c.level == RecoveryLevel.LEVEL_1_RETRY_SAFE


def test_same_exception_after_submission_is_never_auto_retried():
    c = classify_script_exception("ServerTimeoutError", order_submission_attempted=True)
    assert c.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP
    assert c.category == IncidentCategory.AMBIGUOUS_ORDER_STATE


def test_unrecognized_exception_before_submission_defaults_to_critical():
    c = classify_script_exception("SomeWeirdParsingError", order_submission_attempted=False)
    assert c.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP
    assert c.category == IncidentCategory.CODE_LOGIC_UNKNOWN


def test_unrecognized_exception_after_submission_is_also_critical():
    c = classify_script_exception("SomeWeirdParsingError", order_submission_attempted=True)
    assert c.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP


# ---- decide_recovery_plan: structural incident-type path ------------------


def test_capital_rebalance_incident_recalculates_not_stops():
    plan = decide_recovery_plan(incident_type="REBALANCE FAILED (rebalance leg)")
    assert plan.action == RecoveryActionType.RECALCULATE_AND_RETRY
    assert plan.category == IncidentCategory.CAPITAL_REBALANCE
    assert plan.scope == RecoveryScope.SYMBOL_DIRECTION


def test_reconciliation_mismatch_recalculates_with_kb_annotation():
    kb_entry = KB["RECONCILIATION_MISSING_REBALANCE_EVENT"]
    plan = decide_recovery_plan(incident_type="BALANCE / LEDGER MISMATCH", known_incident=kb_entry)
    assert plan.action == RecoveryActionType.RECALCULATE_AND_RETRY
    assert "RECONCILIATION_MISSING_REBALANCE_EVENT" in plan.reason


def test_order_rejected_safe_isolates_only_the_opportunity():
    plan = decide_recovery_plan(incident_type="SELL REJECTED AFTER BUY (neutralized on buy exchange)")
    assert plan.action == RecoveryActionType.ISOLATE_OPPORTUNITY
    assert plan.scope == RecoveryScope.SYMBOL_DIRECTION


def test_repeated_execution_error_isolates_the_exchange_not_globally():
    plan = decide_recovery_plan(incident_type="REPEATED EXECUTION ERROR")
    assert plan.action == RecoveryActionType.ISOLATE_EXCHANGE
    assert plan.scope == RecoveryScope.EXCHANGE


# ---- decide_recovery_plan: LEVEL 5 is never downgraded by a KB match ------


def test_neutralization_failed_is_always_global_stop_even_with_a_kb_match():
    kb_entry = KB["NEUTRALIZATION_QTY_EXCEEDS_FREE_BALANCE"]
    plan = decide_recovery_plan(incident_type="NEUTRALIZATION FAILED -- UNHEDGED POSITION", known_incident=kb_entry)
    assert plan.action == RecoveryActionType.GLOBAL_SAFE_STOP
    assert plan.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP
    assert "more alarming, not less" in plan.reason


def test_unknown_incident_with_no_kb_match_is_code_fix_required():
    plan = decide_recovery_plan(incident_type="A TOTALLY NEW PROBLEM")
    assert plan.action == RecoveryActionType.CODE_FIX_REQUIRED
    assert plan.category == IncidentCategory.CODE_LOGIC_UNKNOWN


def test_script_error_with_a_kb_match_is_global_stop_not_code_fix_required():
    """A KB match means we've SEEN this exact signature before (and
    presumably it's not actually CODE_LOGIC_UNKNOWN anymore, it's a
    recurrence) -- still LEVEL 5, but no longer 'genuinely new', so the
    action is GLOBAL_SAFE_STOP rather than CODE_FIX_REQUIRED."""
    kb_entry = KB["BUY_EXCHANGE_USDT_RESERVE_NOT_CHECKED"]
    plan = decide_recovery_plan(incident_type="SCRIPT ERROR (unexpected exception)", known_incident=kb_entry)
    assert plan.action == RecoveryActionType.GLOBAL_SAFE_STOP


def test_kill_switch_engaged_is_always_global_stop():
    plan = decide_recovery_plan(incident_type="KILL SWITCH ENGAGED (live arbitrage leg)")
    assert plan.action == RecoveryActionType.GLOBAL_SAFE_STOP
    assert plan.scope == RecoveryScope.GLOBAL


def test_reason_includes_kb_safe_recovery_text_for_recoverable_categories():
    kb_entry = KB["RECONCILIATION_MISSING_REBALANCE_EVENT"]
    plan = decide_recovery_plan(incident_type="BALANCE / LEDGER MISMATCH", known_incident=kb_entry)
    assert kb_entry.safe_recovery in plan.reason
