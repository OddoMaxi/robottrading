from app.engines.funding import FUNDING_INTERVAL_SECONDS, funding_events_crossed

NOW = 1_000_000.0
TARGET_HOLD = 9 * FUNDING_INTERVAL_SECONDS  # ~72h, matches the engine's default policy


def test_position_opened_right_after_a_funding_event_crosses_the_expected_count():
    # Next funding due in a full cycle (worst-case phase offset): the 9th
    # payment would land exactly as the position closes, which doesn't
    # count (see the "exactly at" test below) — 8 is correct here, not 9.
    next_funding = NOW + FUNDING_INTERVAL_SECONDS
    assert funding_events_crossed(next_funding, NOW, TARGET_HOLD) == 8


def test_position_opened_right_before_a_funding_event_crosses_the_expected_count():
    next_funding = NOW + 60.0  # 1 minute away
    assert funding_events_crossed(next_funding, NOW, TARGET_HOLD) == 9


def test_position_closing_before_the_next_funding_event_captures_zero():
    next_funding = NOW + TARGET_HOLD + 1.0  # due after the position would have closed
    assert funding_events_crossed(next_funding, NOW, TARGET_HOLD) == 0


def test_position_closing_exactly_at_the_next_funding_event_captures_zero():
    """Section 21's own framing — captures a payment only if the position
    is still open when it *pays out*, not merely open until that instant."""
    next_funding = NOW + TARGET_HOLD
    assert funding_events_crossed(next_funding, NOW, TARGET_HOLD) == 0


def test_short_hold_captures_exactly_one_event_when_it_fits():
    next_funding = NOW + 3600.0  # 1h away
    short_hold = 2 * 3600.0  # 2h target hold, only enough for the first payment
    assert funding_events_crossed(next_funding, NOW, short_hold) == 1


def test_already_past_next_funding_time_is_clamped_to_now():
    """A stale-but-still-fresh-enough funding snapshot might report a
    next_funding_time that's already slipped into the past — treat that as
    "the next payment is imminent", not a negative time-to-first-funding."""
    next_funding = NOW - 30.0
    assert funding_events_crossed(next_funding, NOW, TARGET_HOLD) == 9
