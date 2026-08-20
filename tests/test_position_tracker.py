from app.simulation.position_tracker import OpenPositionTracker


def test_position_not_open_initially():
    tracker = OpenPositionTracker()
    assert tracker.is_open(("basis", "binance", "BTC/USDT"), now=1000.0) is False


def test_position_open_immediately_after_opening():
    tracker = OpenPositionTracker()
    key = ("basis", "binance", "BTC/USDT")
    tracker.open_position(key, now=1000.0, holding_period_seconds=3600.0)
    assert tracker.is_open(key, now=1000.1) is True


def test_position_still_open_within_holding_period():
    tracker = OpenPositionTracker()
    key = ("funding", "okx", "ETH/USDT")
    tracker.open_position(key, now=1000.0, holding_period_seconds=3600.0)
    assert tracker.is_open(key, now=1000.0 + 3599.0) is True


def test_position_closed_after_holding_period():
    tracker = OpenPositionTracker()
    key = ("basis", "binance", "BTC/USDT")
    tracker.open_position(key, now=1000.0, holding_period_seconds=3600.0)
    assert tracker.is_open(key, now=1000.0 + 3601.0) is False


def test_different_keys_are_independent():
    tracker = OpenPositionTracker()
    tracker.open_position(("basis", "binance", "BTC/USDT"), now=1000.0, holding_period_seconds=3600.0)
    assert tracker.is_open(("basis", "binance", "ETH/USDT"), now=1000.1) is False
    assert tracker.is_open(("funding", "binance", "BTC/USDT"), now=1000.1) is False


def test_reopening_extends_expiry():
    tracker = OpenPositionTracker()
    key = ("basis", "binance", "BTC/USDT")
    tracker.open_position(key, now=1000.0, holding_period_seconds=100.0)
    tracker.open_position(key, now=1050.0, holding_period_seconds=100.0)
    assert tracker.is_open(key, now=1120.0) is True  # would have expired at 1100 without the re-open


def test_min_reentry_delay_keeps_the_key_blocked_briefly_after_the_holding_period_ends():
    """Spec section 10 — even once a position's holding period is over, the
    same key stays blocked for min_reentry_delay_seconds before it can be
    re-traded — a real pipeline can't reopen with a literal 0ms gap."""
    tracker = OpenPositionTracker(min_reentry_delay_seconds=1.0)
    key = ("cross_exchange", "binance", "BTC/USDT")
    tracker.open_position(key, now=1000.0, holding_period_seconds=8.0)

    assert tracker.is_open(key, now=1008.5) is True  # within the cooldown window
    assert tracker.is_open(key, now=1009.1) is False  # cooldown has elapsed


def test_min_reentry_delay_defaults_to_a_short_fast_mode_friendly_gap():
    tracker = OpenPositionTracker()  # default min_reentry_delay_seconds=0.5
    key = ("cross_exchange", "binance", "BTC/USDT")
    tracker.open_position(key, now=1000.0, holding_period_seconds=8.0)
    assert tracker.is_open(key, now=1008.4) is True  # holding period over, still in the 0.5s cooldown
    assert tracker.is_open(key, now=1008.6) is False  # cooldown elapsed
