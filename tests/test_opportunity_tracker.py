from app.config.constants import OpportunityStatus, Strategy
from app.opportunity.models import Opportunity
from app.opportunity.tracker import OpportunityTracker, opportunity_key


def make_opp(net_spread_pct: float = 0.30, symbol: str = "BTC/USDT") -> Opportunity:
    return Opportunity(
        strategy=Strategy.CROSS_EXCHANGE,
        symbol=symbol,
        legs=[
            {"exchange": "binance", "side": "buy", "market": "spot"},
            {"exchange": "okx", "side": "sell", "market": "spot"},
        ],
        gross_spread_pct=net_spread_pct + 0.1,
        net_spread_pct=net_spread_pct,
    )


def test_first_sighting_is_new():
    tracker = OpportunityTracker()
    obs = tracker.observe(make_opp(), now=100.0)
    assert obs.is_new is True
    assert obs.tracked.status == OpportunityStatus.DETECTED
    assert obs.tracked.updates_count == 1


def test_repeated_sighting_within_liveness_window_is_a_continuation_not_a_new_row():
    tracker = OpportunityTracker(liveness_seconds=5.0)
    first = tracker.observe(make_opp(0.30), now=100.0)
    second = tracker.observe(make_opp(0.32), now=101.0)

    assert second.is_new is False
    assert second.tracked.opportunity_id == first.tracked.opportunity_id  # same DB row
    assert second.tracked.updates_count == 2
    assert second.tracked.status == OpportunityStatus.ACTIVE


def test_continuation_mutates_the_fresh_opportunitys_id_to_match_the_tracked_row():
    """So paper-trading and position keys downstream naturally refer to the
    one underlying opportunity, not a new id every scan."""
    tracker = OpportunityTracker()
    first_opp = make_opp(0.30)
    first_obs = tracker.observe(first_opp, now=100.0)

    second_opp = make_opp(0.31)
    original_second_id = second_opp.id
    tracker.observe(second_opp, now=101.0)

    assert second_opp.id == first_opp.id
    assert second_opp.id != original_second_id


def test_min_max_avg_and_current_edge_track_correctly_across_updates():
    tracker = OpportunityTracker()
    tracker.observe(make_opp(0.20), now=100.0)
    tracker.observe(make_opp(0.40), now=101.0)
    obs = tracker.observe(make_opp(0.30), now=102.0)

    assert obs.tracked.initial_edge_pct == 0.20
    assert obs.tracked.current_edge_pct == 0.30
    assert obs.tracked.max_edge_pct == 0.40
    assert obs.tracked.min_edge_pct == 0.20
    assert obs.tracked.avg_edge_pct == (0.20 + 0.40 + 0.30) / 3


def test_sighting_after_liveness_window_elapses_is_a_new_opportunity():
    tracker = OpportunityTracker(liveness_seconds=5.0)
    first = tracker.observe(make_opp(), now=100.0)
    second = tracker.observe(make_opp(), now=110.0)  # 10s later, well past the 5s window

    assert second.is_new is True
    assert second.tracked.opportunity_id != first.tracked.opportunity_id


def test_different_symbols_never_collide():
    tracker = OpportunityTracker()
    btc = tracker.observe(make_opp(symbol="BTC/USDT"), now=100.0)
    eth = tracker.observe(make_opp(symbol="ETH/USDT"), now=100.0)
    assert btc.tracked.opportunity_id != eth.tracked.opportunity_id


def test_expire_stale_removes_and_flags_quiet_signals():
    tracker = OpportunityTracker(liveness_seconds=5.0)
    tracker.observe(make_opp(), now=100.0)

    still_fresh = tracker.expire_stale(now=102.0)
    assert still_fresh == []
    assert tracker.active_count() == 1

    expired = tracker.expire_stale(now=110.0)
    assert len(expired) == 1
    assert expired[0].status == OpportunityStatus.EXPIRED
    assert tracker.active_count() == 0


def test_expiring_then_reobserving_creates_a_genuinely_new_opportunity():
    """Spec section 11 — "independent edge detection": once a signal has
    fully faded (expired), its later re-emergence is a new opportunity, not
    a continuation of the old one."""
    tracker = OpportunityTracker(liveness_seconds=5.0)
    first = tracker.observe(make_opp(), now=100.0)
    tracker.expire_stale(now=110.0)

    second = tracker.observe(make_opp(), now=110.5)
    assert second.is_new is True
    assert second.tracked.opportunity_id != first.tracked.opportunity_id


def test_opportunity_key_distinguishes_direction():
    buy_binance = Opportunity(
        strategy=Strategy.CROSS_EXCHANGE, symbol="BTC/USDT", legs=[{"exchange": "binance", "side": "buy", "market": "spot"}], gross_spread_pct=0.1
    )
    sell_binance = Opportunity(
        strategy=Strategy.CROSS_EXCHANGE, symbol="BTC/USDT", legs=[{"exchange": "binance", "side": "sell", "market": "spot"}], gross_spread_pct=0.1
    )
    assert opportunity_key(buy_binance) != opportunity_key(sell_binance)
