import pytest

from app.operations.incident_taxonomy import (
    IncidentCategory,
    RecoveryLevel,
    RecoveryScope,
    classify_incident,
)

# ---- exact real incident-type strings, taken verbatim from
# continuous_live_session_v2.py's own incident dicts and
# execute_rebalance_sell's error strings. -------------------------------


@pytest.mark.parametrize(
    "incident_type,expected_category,expected_level,expected_scope",
    [
        ("API PERMISSION CHANGE / WITHDRAWALS UNEXPECTEDLY ENABLED", IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
        ("NEUTRALIZATION FAILED -- UNHEDGED POSITION", IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
        ("REBALANCE COULD NOT RESTORE THE FLOOR (rebalance leg)", IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
        ("CAPITAL SAFETY VIOLATION", IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
        ("SESSION LOSS LIMIT REACHED", IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
        ("UNEXPLAINED P&L DISCREPANCY (prediction error exceeds threshold)", IncidentCategory.CODE_LOGIC_UNKNOWN, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
        ("UNKNOWN ORDER STATE (buy leg)", IncidentCategory.AMBIGUOUS_ORDER_STATE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION),
        ("UNKNOWN ORDER STATE (sell leg)", IncidentCategory.AMBIGUOUS_ORDER_STATE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION),
        ("UNKNOWN ORDER STATE (rebalance leg)", IncidentCategory.AMBIGUOUS_ORDER_STATE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION),
        ("BALANCE / LEDGER MISMATCH", IncidentCategory.RECONCILIATION, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION),
        ("SELL REJECTED AFTER BUY (neutralized on buy exchange)", IncidentCategory.ORDER_REJECTED_SAFE, RecoveryLevel.LEVEL_3_ISOLATE_OPPORTUNITY, RecoveryScope.SYMBOL_DIRECTION),
        ("REBALANCE FAILED (rebalance leg)", IncidentCategory.CAPITAL_REBALANCE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION),
        ("REPEATED EXECUTION ERROR", IncidentCategory.RECOVERABLE_OPERATIONAL, RecoveryLevel.LEVEL_4_ISOLATE_EXCHANGE, RecoveryScope.EXCHANGE),
        ("SCRIPT ERROR (unexpected exception during a real order/inventory/rebalance step -- immediate stop, no retry)", IncidentCategory.CODE_LOGIC_UNKNOWN, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL),
    ],
)
def test_real_incident_type_strings_classify_as_expected(incident_type, expected_category, expected_level, expected_scope):
    result = classify_incident(incident_type)
    assert result.category == expected_category
    assert result.level == expected_level
    assert result.scope == expected_scope


def test_kill_switch_dominates_over_unknown_order_state_in_the_combined_real_string():
    """The real inventory-leg incident type is literally 'UNKNOWN ORDER
    STATE / KILL SWITCH ENGAGED (inventory leg)' -- KILL SWITCH ENGAGED
    must win (LEVEL 5), not the more lenient UNKNOWN ORDER STATE
    (LEVEL 2) rule that would otherwise match first as a substring."""
    result = classify_incident("UNKNOWN ORDER STATE / KILL SWITCH ENGAGED (inventory leg)")
    assert result.category == IncidentCategory.CRITICAL_SAFETY
    assert result.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP
    assert result.auto_recoverable is False


def test_bare_kill_switch_engaged_is_critical():
    result = classify_incident("KILL SWITCH ENGAGED (live arbitrage leg)")
    assert result.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP


def test_fee_asset_unknown_is_critical_not_recalculate():
    result = classify_incident("FEE ASSET UNKNOWN (arbitrage leg)")
    assert result.category == IncidentCategory.CRITICAL_SAFETY
    assert result.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP


def test_unrecognized_incident_type_defaults_to_the_safest_outcome():
    result = classify_incident("SOME BRAND NEW PROBLEM NEVER SEEN BEFORE")
    assert result.category == IncidentCategory.CODE_LOGIC_UNKNOWN
    assert result.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP
    assert result.scope == RecoveryScope.GLOBAL
    assert result.auto_recoverable is False


def test_auto_recoverable_is_true_for_every_level_below_5():
    for level in (
        RecoveryLevel.LEVEL_0_IGNORE, RecoveryLevel.LEVEL_1_RETRY_SAFE, RecoveryLevel.LEVEL_2_RECALCULATE,
        RecoveryLevel.LEVEL_3_ISOLATE_OPPORTUNITY, RecoveryLevel.LEVEL_4_ISOLATE_EXCHANGE,
    ):
        from app.operations.incident_taxonomy import IncidentClassification
        c = IncidentClassification(IncidentCategory.TRANSIENT, level, RecoveryScope.NONE, "test")
        assert c.auto_recoverable is True


def test_recovery_levels_are_ordered_for_severity_comparisons():
    assert RecoveryLevel.LEVEL_0_IGNORE < RecoveryLevel.LEVEL_3_ISOLATE_OPPORTUNITY < RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP
