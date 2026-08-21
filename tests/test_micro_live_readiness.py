from app.reporting.micro_live_readiness import ReadinessVerdict, build_readiness_report


def _all_pass_kwargs() -> dict:
    return dict(
        ledger_reconciled=True,
        ledger_violations=[],
        any_available_capital_negative=False,
        any_utilization_over_100_pct=False,
        kill_switch_engaged=False,
        reality_capture_ratio_pct=75.0,
        net_profit_usd=120.0,
        max_drawdown_pct=5.0,
        robustness_score=80.0,
        testnet_reachable=True,
    )


def test_ready_when_every_check_passes():
    report = build_readiness_report(**_all_pass_kwargs())
    assert report.verdict == ReadinessVerdict.READY_FOR_CONTROLLED_TEST
    assert report.failed_checks == []


def test_not_ready_when_ledger_is_unreconciled():
    kwargs = _all_pass_kwargs()
    kwargs["ledger_reconciled"] = False
    kwargs["ledger_violations"] = ["5K: equity disagrees with the DB ledger"]
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.NOT_READY
    assert any(c.name == "ledger_healthy" for c in report.failed_checks)


def test_not_ready_when_capital_is_negative():
    kwargs = _all_pass_kwargs()
    kwargs["any_available_capital_negative"] = True
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.NOT_READY
    assert any(c.name == "no_negative_capital" for c in report.failed_checks)


def test_not_ready_when_kill_switch_is_engaged():
    kwargs = _all_pass_kwargs()
    kwargs["kill_switch_engaged"] = True
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.NOT_READY
    assert any(c.name == "kill_switch_disengaged" for c in report.failed_checks)


def test_not_ready_when_net_pnl_is_not_positive():
    kwargs = _all_pass_kwargs()
    kwargs["net_profit_usd"] = -5.0
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.NOT_READY
    assert any(c.name == "positive_net_pnl" for c in report.failed_checks)


def test_missing_data_fails_its_check_rather_than_passing_vacuously():
    """No trade history yet shouldn't be silently treated as 'fine' —
    NOT_READY is the honest answer until there's enough data to know."""
    kwargs = _all_pass_kwargs()
    kwargs["reality_capture_ratio_pct"] = None
    kwargs["net_profit_usd"] = None
    kwargs["max_drawdown_pct"] = None
    kwargs["robustness_score"] = None
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.NOT_READY
    failed_names = {c.name for c in report.failed_checks}
    assert {"reality_capture_stable", "positive_net_pnl", "acceptable_drawdown", "stress_test_positive"} <= failed_names


def test_not_ready_when_testnet_is_unreachable():
    kwargs = _all_pass_kwargs()
    kwargs["testnet_reachable"] = False
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.NOT_READY
    assert any(c.name == "testnet_reachable" for c in report.failed_checks)


def test_borderline_thresholds_pass_at_exactly_the_boundary():
    kwargs = _all_pass_kwargs()
    kwargs["reality_capture_ratio_pct"] = 50.0
    kwargs["max_drawdown_pct"] = 20.0
    kwargs["robustness_score"] = 50.0
    report = build_readiness_report(**kwargs)
    assert report.verdict == ReadinessVerdict.READY_FOR_CONTROLLED_TEST
