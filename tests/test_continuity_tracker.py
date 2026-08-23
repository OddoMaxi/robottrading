from app.scanner.continuity_tracker import ContinuityTracker


def test_first_positive_tick_is_new():
    tracker = ContinuityTracker()
    result = tracker.observe("ZRO/USDT", "binance", "bybit", True, now=0.0)
    assert result == "new"


def test_second_consecutive_positive_tick_is_continuation():
    tracker = ContinuityTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=0.0)
    result = tracker.observe("ZRO/USDT", "binance", "bybit", True, now=5.0)
    assert result == "continuation"


def test_negative_tick_resets_the_streak():
    tracker = ContinuityTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=0.0)
    result = tracker.observe("ZRO/USDT", "binance", "bybit", False, now=5.0)
    assert result == "none"
    # a positive tick after a gap is NEW again, not a continuation
    result = tracker.observe("ZRO/USDT", "binance", "bybit", True, now=10.0)
    assert result == "new"


def test_different_directions_tracked_independently():
    tracker = ContinuityTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=0.0)
    result = tracker.observe("ZRO/USDT", "bybit", "binance", True, now=0.0)
    assert result == "new"  # the reverse direction is a separate key


def test_current_persistence_seconds_tracks_ongoing_streak():
    tracker = ContinuityTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=100.0)
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=110.0)
    assert tracker.current_persistence_seconds("ZRO/USDT", "binance", "bybit", now=115.0) == 15.0


def test_current_persistence_seconds_zero_when_not_currently_positive():
    tracker = ContinuityTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=0.0)
    tracker.observe("ZRO/USDT", "binance", "bybit", False, now=5.0)
    assert tracker.current_persistence_seconds("ZRO/USDT", "binance", "bybit", now=10.0) == 0.0


def test_summary_counts_detections_and_continuations():
    tracker = ContinuityTracker()
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=0.0)  # new (detection)
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=5.0)  # continuation
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=10.0)  # continuation
    tracker.observe("ZRO/USDT", "binance", "bybit", False, now=15.0)  # streak ends
    tracker.observe("ZRO/USDT", "binance", "bybit", True, now=20.0)  # new (detection) again

    summary = tracker.summary("ZRO/USDT", "binance", "bybit")
    assert summary.detections == 2
    assert summary.continuations == 2
    assert summary.total_positive_ticks == 4
    assert summary.longest_streak_seconds == 10.0
