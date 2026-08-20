from app.market_data.quality import (
    FeedHealth,
    blocks_new_execution,
    build_feed_status,
    classify_feed_health,
)


def test_healthy_within_normal_cadence():
    assert classify_feed_health(1.0, expected_cadence_seconds=2.0) == FeedHealth.HEALTHY


def test_degraded_at_a_couple_missed_cadences():
    assert classify_feed_health(5.0, expected_cadence_seconds=2.0) == FeedHealth.DEGRADED


def test_stale_at_several_missed_cadences():
    assert classify_feed_health(10.0, expected_cadence_seconds=2.0) == FeedHealth.STALE


def test_broken_at_many_missed_cadences():
    assert classify_feed_health(30.0, expected_cadence_seconds=2.0) == FeedHealth.BROKEN


def test_missing_data_entirely_is_broken():
    assert classify_feed_health(None, expected_cadence_seconds=2.0) == FeedHealth.BROKEN


def test_slow_polled_feed_isnt_falsely_flagged_at_its_normal_cadence():
    """A funding feed polled every 30s shouldn't be DEGRADED at 35s — that's
    just normal polling timing, not a data quality problem."""
    assert classify_feed_health(35.0, expected_cadence_seconds=30.0) == FeedHealth.HEALTHY


def test_slow_polled_feed_does_go_stale_after_several_missed_polls():
    assert classify_feed_health(150.0, expected_cadence_seconds=30.0) == FeedHealth.STALE


def test_blocks_new_execution_only_for_stale_or_broken():
    assert blocks_new_execution(FeedHealth.HEALTHY) is False
    assert blocks_new_execution(FeedHealth.DEGRADED) is False
    assert blocks_new_execution(FeedHealth.STALE) is True
    assert blocks_new_execution(FeedHealth.BROKEN) is True


def test_build_feed_status_computes_age_and_health_together():
    status = build_feed_status("binance", "BTC/USDT", "funding", last_update_at=1_000.0, expected_cadence_seconds=30.0, now=1_035.0)
    assert status.age_seconds == 35.0
    assert status.health == FeedHealth.HEALTHY


def test_build_feed_status_handles_never_updated():
    status = build_feed_status("binance", "BTC/USDT", "funding", last_update_at=None, expected_cadence_seconds=30.0, now=1_035.0)
    assert status.age_seconds is None
    assert status.health == FeedHealth.BROKEN
