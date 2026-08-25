from app.execution.reconciliation import LedgerEvent, LedgerEventType, reconcile_asset_balance
from app.operations.reconciliation_self_healing import attempt_reconciliation_recovery


def _event(event_type, exchange, base_asset, net_base_delta, *, side="SELL", order_id=None) -> LedgerEvent:
    return LedgerEvent(
        event_type=event_type, exchange=exchange, base_asset=base_asset, quote_asset="USDT", side=side,
        gross_base_delta=net_base_delta, net_base_delta=net_base_delta, quote_delta=None, fee_asset=None,
        fee_amount=None, order_id=order_id, timestamp=None,
    )


def test_already_matching_result_needs_no_healing():
    original = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=10.0, after_balance=10.0, events=())
    result = attempt_reconciliation_recovery(original_result=original, candidate_events=())
    assert result.recovered is True
    assert result.explaining_events == ()
    assert result.cross_asset_attempts_rejected == ()
    assert result.final_result is original


def test_rvn_incident_is_auto_recovered_by_the_real_same_asset_rebalance_event():
    """The original RVN incident (both events for the SAME asset, RVN):
    self-healing must discover the exact real explanation (the 2192.5
    RVN rebalance sell, order 1344692249) given only the ORIGINAL
    (pre-FIX-3-shaped) reconciliation call that didn't know about it."""
    original = reconcile_asset_balance(
        exchange="binance", asset="RVN", before_balance=14400.3175, after_balance=14332.9902,
        events=(_event(LedgerEventType.ARBITRAGE_BUY, "binance", "RVN", 2125.1727, side="BUY"),),
    )
    assert original.match is False  # the real incident, reproduced

    candidates = (_event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2192.5, order_id="1344692249"),)
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)

    assert healed.recovered is True
    assert healed.final_result.match is True
    assert "1344692249" in healed.diagnostic
    assert len(healed.explaining_events) == 1
    assert healed.cross_asset_attempts_rejected == ()


def test_cross_asset_candidate_is_never_used_even_if_it_would_numerically_fit():
    """The ZIL/RVN incident's shape, exercised directly at this layer:
    a same-exchange candidate for a DIFFERENT asset (RVN) that would
    numerically close a ZIL gap must be rejected and reported as
    CROSS_ASSET_RECONCILIATION_ATTEMPT, never applied as an
    explanation."""
    original = reconcile_asset_balance(
        exchange="binance", asset="ZIL", before_balance=6202.0917, after_balance=8828.3628,
        events=(),  # deliberately omit the real ZIL arbitrage-buy event too, to isolate this check
    )
    assert original.match is False

    wrong_asset_candidate = (_event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2107.9),)
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=wrong_asset_candidate)

    assert healed.recovered is False
    assert healed.final_result.match is False
    assert len(healed.cross_asset_attempts_rejected) == 1
    assert healed.cross_asset_attempts_rejected[0].base_asset == "RVN"
    assert "CROSS_ASSET_RECONCILIATION_ATTEMPT" in healed.diagnostic


def test_cross_asset_candidate_is_rejected_even_when_a_valid_same_asset_candidate_also_exists():
    """A mixed candidate list: one real same-asset event that genuinely
    explains the gap, and one cross-asset event that happens to be
    present too (e.g. a rebalance for another holding on the same
    exchange, same cycle). The healing must use only the same-asset one
    and still explicitly report the cross-asset one as rejected, not
    silently drop it."""
    original = reconcile_asset_balance(
        exchange="binance", asset="ZIL", before_balance=6202.0917, after_balance=8828.3628,
        events=(),
    )
    candidates = (
        _event(LedgerEventType.ARBITRAGE_BUY, "binance", "ZIL", 2626.2711, side="BUY"),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2107.9),  # different asset, same exchange -- must be rejected
    )
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)

    assert healed.recovered is True
    assert healed.final_result.match is True
    assert len(healed.explaining_events) == 1
    assert healed.explaining_events[0].base_asset == "ZIL"
    assert len(healed.cross_asset_attempts_rejected) == 1
    assert healed.cross_asset_attempts_rejected[0].base_asset == "RVN"
    assert "CROSS_ASSET_RECONCILIATION_ATTEMPT" in healed.diagnostic


def test_smallest_explaining_subset_is_preferred():
    original = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=100.0, after_balance=70.0, events=())
    candidates = (
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -30.0),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", 5.0),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -5.0),
    )
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)
    assert healed.recovered is True
    assert len(healed.explaining_events) == 1
    assert healed.explaining_events[0].net_base_delta == -30.0


def test_unexplainable_mismatch_is_a_genuine_safe_stop():
    original = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=100.0, after_balance=1.0, events=())
    candidates = (
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -2.0),
        _event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", -1.0),
    )
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)
    assert healed.recovered is False
    assert healed.final_result.match is False
    assert "SAFE STOP" in healed.diagnostic
    assert healed.explaining_events == ()


def test_no_candidate_events_and_a_real_mismatch_is_a_safe_stop():
    original = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=100.0, after_balance=1.0, events=())
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=())
    assert healed.recovered is False


def test_never_loosens_the_original_tolerance():
    original = reconcile_asset_balance(exchange="binance", asset="RVN", before_balance=100.0, after_balance=100.0, events=(), tolerance_abs=0.01)
    candidates = (_event(LedgerEventType.REBALANCE_SELL, "binance", "RVN", 5.0),)
    healed = attempt_reconciliation_recovery(original_result=original, candidate_events=candidates)
    assert healed.recovered is True  # original already matched (delta=0 within tolerance)
    assert healed.explaining_events == ()  # and the oversized candidate must not be spuriously applied
