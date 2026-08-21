from datetime import datetime, timedelta

import pytest

from app.reporting.simple_summary import (
    RobotHealth,
    RobotStatus,
    _aggregate_reality_capture,
    _classify_trade_status,
    _reconstruct_open_positions,
    build_explainer_narrative,
    classify_robot_health,
    compute_max_drawdown_usd,
    compute_profit_factor,
    pick_robot_state_message,
)


def test_robot_health_running_when_fresh_and_all_exchanges_connected():
    assert classify_robot_health(1.0, {"binance": True, "okx": True, "bybit": True}) == RobotHealth.RUNNING


def test_robot_health_degraded_when_one_exchange_disconnected():
    assert classify_robot_health(1.0, {"binance": True, "okx": False, "bybit": True}) == RobotHealth.DEGRADED


def test_robot_health_degraded_when_no_data_yet():
    assert classify_robot_health(None, {}) == RobotHealth.DEGRADED


def test_robot_health_down_when_detection_loop_is_stale():
    assert classify_robot_health(300.0, {"binance": True, "okx": True, "bybit": True}) == RobotHealth.DOWN


def test_robot_health_down_when_every_exchange_disconnected():
    assert classify_robot_health(1.0, {"binance": False, "okx": False, "bybit": False}) == RobotHealth.DOWN


def test_max_drawdown_zero_on_monotonic_gains():
    assert compute_max_drawdown_usd([5_000, 5_010, 5_030, 5_050]) == 0.0


def test_max_drawdown_finds_the_largest_peak_to_trough_drop():
    # peak 5050 -> trough 5000 is the worst drop (-50), even though the
    # curve later recovers and a smaller dip (5030 -> 5010, -20) happens too.
    assert compute_max_drawdown_usd([5_000, 5_050, 5_010, 5_030, 5_000, 5_040]) == -50.0


def test_max_drawdown_empty_curve():
    assert compute_max_drawdown_usd([]) == 0.0


def _status(health: RobotHealth, connected: dict[str, bool] | None = None) -> RobotStatus:
    return RobotStatus(health=health, exchanges_connected=connected or {"binance": True, "okx": True, "bybit": True}, last_opportunity_age_seconds=1.0)


def test_robot_state_down_overrides_everything_else():
    msg = pick_robot_state_message(_status(RobotHealth.DOWN, {"binance": False, "okx": True, "bybit": True}), opportunities_today=50, profitable_today=10)
    assert msg.tone == "bad"
    assert "Binance" in msg.body


def test_robot_state_low_opportunity_count_when_healthy_but_quiet():
    msg = pick_robot_state_message(_status(RobotHealth.RUNNING), opportunities_today=0, profitable_today=0)
    assert msg.tone == "warn"
    assert "faibles" in msg.title.lower()


def test_robot_state_fees_too_high_when_opportunities_found_but_none_profitable():
    msg = pick_robot_state_message(_status(RobotHealth.RUNNING), opportunities_today=17, profitable_today=0)
    assert msg.tone == "warn"
    assert "frais" in msg.title.lower()


def test_robot_state_good_when_healthy_and_profitable():
    msg = pick_robot_state_message(_status(RobotHealth.RUNNING), opportunities_today=17, profitable_today=5)
    assert msg.tone == "good"


def test_explainer_narrative_matches_spec_example_shape():
    narrative = build_explainer_narrative(observed=18430, valid=163, executed=41, winning=31, net_pnl_usd=27.60)
    assert "18 430" in narrative
    assert "163" in narrative
    assert "41" in narrative
    assert "31" in narrative
    assert "+27.60 $" in narrative


def test_explainer_narrative_handles_zero_observed():
    assert "pas encore" in build_explainer_narrative(observed=0, valid=0, executed=0, winning=0, net_pnl_usd=0.0).lower()


# --- Urgent audit fix: the dashboard's own reconstruction must respect the
# same invariant as the live engine (VirtualPortfolio.lock_capital) — never
# imply negative available capital or >100% utilization. ---

NOW = datetime(2026, 8, 20, 12, 0, 0)


def _row(capital_usd, net_profit_usd, hours_ago, strategy="basis", symbol="BTC/USDT", exchange="binance", holding_days=35, status="simulated_executed"):
    return (
        capital_usd,
        net_profit_usd,
        NOW - timedelta(hours=hours_ago),
        status,
        strategy,
        symbol,
        [{"exchange": exchange, "side": "buy", "market": "spot"}],
        holding_days * 86400.0,
    )


def test_reconstruct_keeps_a_position_that_fits_within_capital():
    rows = [_row(1000.0, 3.0, hours_ago=1)]
    kept = _reconstruct_open_positions(rows, total_capital_usd=5000.0, now=NOW)
    assert len(kept) == 1


def test_reconstruct_excludes_a_position_sized_over_total_capital():
    """The exact bug found in production: a $5,000 basis position sized
    under since-superseded risk rules, reconstructed against a $500
    portfolio — must be excluded, not shown as negative available capital."""
    rows = [_row(5000.0, 15.0, hours_ago=1, symbol="BTC/USDT")]
    kept = _reconstruct_open_positions(rows, total_capital_usd=500.0, now=NOW)
    assert kept == []


def test_reconstruct_never_lets_cumulative_engaged_exceed_total_capital():
    rows = [
        _row(3000.0, 5.0, hours_ago=3, symbol="BTC/USDT"),
        _row(3000.0, 5.0, hours_ago=2, symbol="ETH/USDT"),  # together with BTC this would be 6,005 > 5,000
    ]
    kept = _reconstruct_open_positions(rows, total_capital_usd=5000.0, now=NOW)
    assert len(kept) == 1
    assert kept[0][4] == "BTC/USDT"  # the earlier one wins, later one excluded
    engaged = sum(c + p for c, p, *_ in [(k[0], k[1]) for k in kept])
    assert engaged <= 5000.0


def test_reconstruct_deduplicates_restart_amnesia_duplicates_keeping_the_earliest():
    rows = [
        _row(1000.0, 3.0, hours_ago=5, symbol="BTC/USDT"),
        _row(1000.0, 3.0, hours_ago=3, symbol="BTC/USDT"),  # same key, opened "again" — restart bug artifact
        _row(1000.0, 3.0, hours_ago=1, symbol="BTC/USDT"),
    ]
    kept = _reconstruct_open_positions(rows, total_capital_usd=5000.0, now=NOW)
    assert len(kept) == 1
    assert kept[0][2] == NOW - timedelta(hours=5)  # the earliest executed_at


def test_reconstruct_excludes_already_closed_positions():
    rows = [_row(1000.0, 3.0, hours_ago=1000, holding_days=1)]  # opened ~41 days ago, 1-day hold — long closed
    kept = _reconstruct_open_positions(rows, total_capital_usd=5000.0, now=NOW)
    assert kept == []


def test_reconstruct_treats_a_time_stop_exit_as_closing_the_position_immediately():
    """FAST TRADING ONLY (2026-08-21) — a basis-style position with a
    36-day nominal hold, force-exited by time_stop 2 hours ago: must show
    as closed *now*, not still "open" for the next 34 days, even though
    the opening trade's own holding_period_seconds says otherwise."""
    rows = [
        _row(5000.0, 15.77, hours_ago=48, holding_days=36),  # the original open, 2 days ago
        _row(5000.0, -12.5, hours_ago=2, status="time_stop_exit"),  # forced out 2 hours ago
    ]
    kept = _reconstruct_open_positions(rows, total_capital_usd=5196.0, now=NOW)
    assert kept == []


def test_reconstruct_a_time_stop_exit_in_the_future_relative_to_now_still_keeps_it_open():
    """Sanity check on the override direction: if the force-close event
    hasn't happened *yet* relative to `now`, the position must still show
    open — this isn't a blanket "ignore holding_period_seconds", only an
    override once the closing event has actually occurred."""
    rows = [
        _row(5000.0, 15.77, hours_ago=1, holding_days=36),
        _row(5000.0, -12.5, hours_ago=-1, status="time_stop_exit"),  # "closes" 1h in the future
    ]
    kept = _reconstruct_open_positions(rows, total_capital_usd=5196.0, now=NOW)
    assert len(kept) == 1


# --- Urgent audit item 4: Closed / Winning / Losing / Open / Failed ---


def test_classify_non_executed_status_is_failed():
    assert _classify_trade_status("missed", 0.0, NOW - timedelta(hours=1), 8.0, NOW) == "failed"
    assert _classify_trade_status("no_capital_available", 0.0, NOW - timedelta(hours=1), 8.0, NOW) == "failed"
    assert _classify_trade_status("max_concurrent_positions", 0.0, NOW - timedelta(hours=1), 8.0, NOW) == "failed"


def test_classify_still_within_holding_period_is_open():
    assert _classify_trade_status("simulated_executed", 3.0, NOW - timedelta(seconds=4), 8.0, NOW) == "open"


def test_classify_past_holding_period_with_profit_is_winning():
    assert _classify_trade_status("simulated_executed", 3.0, NOW - timedelta(seconds=10), 8.0, NOW) == "winning"


def test_classify_past_holding_period_with_loss_is_losing():
    assert _classify_trade_status("simulated_executed", -2.5, NOW - timedelta(seconds=10), 8.0, NOW) == "losing"


def test_classify_emergency_unwind_counts_as_a_realized_loss():
    assert _classify_trade_status("emergency_unwind", -2.5, NOW - timedelta(seconds=10), 8.0, NOW) == "losing"


# --- Reality Engine spec, sections 3-4: Potential / Expected / Realistic, Reality Capture Ratio ---


def test_reality_capture_matches_spec_worked_example():
    # capital=1000, gross_spread_pct=0.30 -> potential = $3.00; realistic booked = $1.80 -> 60% capture.
    rows = [(1000.0, 1.80, 0.30, 2.20, 1000.0)]
    report = _aggregate_reality_capture(rows)
    assert report.potential_usd == pytest.approx(3.00)
    assert report.expected_usd == pytest.approx(2.20)
    assert report.realistic_usd == pytest.approx(1.80)
    assert report.capture_ratio_pct == pytest.approx(60.0)


def test_reality_capture_scales_expected_and_potential_for_a_partial_fill():
    # Opportunity priced at $1,000 capital but only $500 actually filled —
    # potential/expected must scale down to match, not stay at full size.
    rows = [(500.0, 0.90, 0.30, 3.00, 1000.0)]
    report = _aggregate_reality_capture(rows)
    assert report.potential_usd == pytest.approx(1.50)  # 500 * 0.30%
    assert report.expected_usd == pytest.approx(1.50)  # 3.00 * (500/1000)


def test_reality_capture_can_be_negative_when_realistic_pnl_is_negative():
    rows = [(500.0, -0.75, 0.20, 1.00, 500.0)]
    report = _aggregate_reality_capture(rows)
    assert report.realistic_usd == pytest.approx(-0.75)
    assert report.capture_ratio_pct < 0


def test_reality_capture_zero_potential_gives_zero_ratio_not_a_crash():
    rows = [(500.0, 0.0, 0.0, 0.0, 500.0)]
    report = _aggregate_reality_capture(rows)
    assert report.capture_ratio_pct == 0.0


def test_reality_capture_empty_rows():
    report = _aggregate_reality_capture([])
    assert report.potential_usd == 0.0
    assert report.realistic_usd == 0.0
    assert report.capture_ratio_pct == 0.0
    assert report.trade_count == 0


# --- Reality Engine spec, section 34: Profit Factor ---


def test_profit_factor_matches_the_textbook_ratio():
    # $10 won across winners, $4 lost across losers -> 2.5
    assert compute_profit_factor([5.0, 5.0, -2.0, -2.0]) == pytest.approx(2.5)


def test_profit_factor_none_when_there_are_no_losing_trades():
    """Undefined (would be a divide-by-zero), not infinite or zero — the
    caller must render "—", never a fabricated number."""
    assert compute_profit_factor([5.0, 3.0, 0.0]) is None


def test_profit_factor_below_one_means_losses_outweigh_wins():
    assert compute_profit_factor([1.0, -5.0]) == pytest.approx(0.2)


def test_profit_factor_empty_list_is_none():
    assert compute_profit_factor([]) is None
