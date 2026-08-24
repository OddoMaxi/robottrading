from dataclasses import dataclass

from app.scanner.missed_opportunity_tracker import (
    CAUSE_FEES,
    CAUSE_INSUFFICIENT_DEPTH,
    CAUSE_LATENCY,
    CAUSE_MIN_NOTIONAL,
    CAUSE_OTHER,
    CAUSE_SAFETY_MARGIN,
    EdgeDisappearanceTracker,
    MissedOpportunityTracker,
    classify_miss,
)


@dataclass
class _FakeQuote:
    executable: bool
    net_profit_usd: float
    reason: str | None
    buy_slippage_pct: float = 0.0
    sell_slippage_pct: float = 0.0
    dual_leg_latency_ms: float = 100.0


# ---- classify_miss -------------------------------------------------------


def test_insufficient_depth_takes_priority_even_if_executable():
    q = _FakeQuote(executable=True, net_profit_usd=0.05, reason=None, buy_slippage_pct=100.0)
    cause, theoretical = classify_miss(q, safety_margin_usd=0.0)
    assert cause == CAUSE_INSUFFICIENT_DEPTH
    assert theoretical == 0.05


def test_min_notional_rejection():
    q = _FakeQuote(executable=False, net_profit_usd=0.0, reason="buy notional 2.0000 below min_notional 5.0")
    cause, theoretical = classify_miss(q, safety_margin_usd=0.0)
    assert cause == CAUSE_MIN_NOTIONAL


def test_lot_size_rejection_maps_to_min_notional_bucket():
    q = _FakeQuote(executable=False, net_profit_usd=0.0, reason="buy leg quantity 0.001 below min_qty 0.01")
    cause, _ = classify_miss(q, safety_margin_usd=0.0)
    assert cause == CAUSE_MIN_NOTIONAL


def test_not_tradable_maps_to_other():
    q = _FakeQuote(executable=False, net_profit_usd=0.0, reason="buy leg (binance) not tradable")
    cause, _ = classify_miss(q, safety_margin_usd=0.0)
    assert cause == CAUSE_OTHER


def test_negative_net_profit_after_fees_maps_to_fees():
    q = _FakeQuote(executable=False, net_profit_usd=-0.01, reason="net_profit_usd is -0.010000 (<= 0) after both legs' real fees/slippage")
    cause, theoretical = classify_miss(q, safety_margin_usd=0.0)
    assert cause == CAUSE_FEES
    assert theoretical == 0.0  # nothing positive to report as not-realized


def test_executable_but_below_safety_margin():
    q = _FakeQuote(executable=True, net_profit_usd=0.01, reason=None)
    cause, theoretical = classify_miss(q, safety_margin_usd=0.02)
    assert cause == CAUSE_SAFETY_MARGIN
    assert theoretical == 0.01


def test_executable_clears_safety_margin_but_too_slow():
    q = _FakeQuote(executable=True, net_profit_usd=0.05, reason=None, dual_leg_latency_ms=5000.0)
    cause, theoretical = classify_miss(q, safety_margin_usd=0.0, latency_threshold_ms=2000.0)
    assert cause == CAUSE_LATENCY
    assert theoretical == 0.05


def test_genuinely_actionable_opportunity_is_not_a_miss():
    q = _FakeQuote(executable=True, net_profit_usd=0.05, reason=None, dual_leg_latency_ms=200.0)
    cause, theoretical = classify_miss(q, safety_margin_usd=0.01, latency_threshold_ms=2000.0)
    assert cause is None
    assert theoretical == 0.05


def test_unrecognized_rejection_reason_falls_back_to_other():
    q = _FakeQuote(executable=False, net_profit_usd=0.0, reason="some new check that didn't exist when this classifier was written")
    cause, _ = classify_miss(q, safety_margin_usd=0.0)
    assert cause == CAUSE_OTHER


# ---- MissedOpportunityTracker --------------------------------------------


def test_tracker_accumulates_count_and_profit_per_cause():
    tracker = MissedOpportunityTracker()
    tracker.record(CAUSE_FEES, 0.0)
    tracker.record(CAUSE_SAFETY_MARGIN, 0.02)
    tracker.record(CAUSE_SAFETY_MARGIN, 0.03)
    snap = tracker.snapshot()
    assert snap[CAUSE_FEES].count == 1
    assert snap[CAUSE_SAFETY_MARGIN].count == 2
    assert snap[CAUSE_SAFETY_MARGIN].theoretical_profit_usd_total == 0.05


def test_tracker_never_records_negative_theoretical_profit():
    tracker = MissedOpportunityTracker()
    tracker.record(CAUSE_FEES, -5.0)  # defensive: even if a caller passes something negative, never let it subtract
    assert tracker.snapshot()[CAUSE_FEES].theoretical_profit_usd_total == 0.0


def test_snapshot_is_a_copy_not_a_live_view():
    tracker = MissedOpportunityTracker()
    tracker.record(CAUSE_FEES, 1.0)
    snap = tracker.snapshot()
    tracker.record(CAUSE_FEES, 1.0)
    assert snap[CAUSE_FEES].count == 1  # the earlier snapshot must not see the later record()


# ---- EdgeDisappearanceTracker ---------------------------------------------


def test_no_disappearance_on_first_positive_tick():
    tracker = EdgeDisappearanceTracker()
    result = tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=True, net_profit_usd=0.05)
    assert result is None


def test_disappearance_detected_after_a_positive_streak_ends():
    tracker = EdgeDisappearanceTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=True, net_profit_usd=0.05)
    tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=True, net_profit_usd=0.08)
    result = tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=False, net_profit_usd=-0.01)
    assert result == 0.08  # the LAST positive value before it disappeared


def test_no_disappearance_reported_twice_in_a_row():
    tracker = EdgeDisappearanceTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=True, net_profit_usd=0.05)
    first = tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=False, net_profit_usd=-0.01)
    second = tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=False, net_profit_usd=-0.02)
    assert first == 0.05
    assert second is None


def test_different_directions_tracked_independently():
    tracker = EdgeDisappearanceTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=True, net_profit_usd=0.05)
    tracker.observe("ZRO/USDT", "bybit", "binance", is_positive=True, net_profit_usd=0.09)
    result = tracker.observe("ZRO/USDT", "binance", "bybit", is_positive=False, net_profit_usd=-0.01)
    assert result == 0.05
    # the reverse direction's streak must be untouched
    still_positive = tracker.observe("ZRO/USDT", "bybit", "binance", is_positive=True, net_profit_usd=0.10)
    assert still_positive is None
