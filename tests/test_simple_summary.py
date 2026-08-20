from app.reporting.simple_summary import (
    RobotHealth,
    RobotStatus,
    build_explainer_narrative,
    classify_robot_health,
    compute_max_drawdown_usd,
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
