from app.reporting.dex_reality import _build_report


def test_capture_ratio_is_realistic_over_theoretical():
    report = _build_report("dex_cross", 10, 1.0, 0.25)
    assert report.capture_ratio_pct == 25.0


def test_zero_theoretical_edge_never_divides_by_zero():
    report = _build_report("dex_cross", 0, 0.0, None)
    assert report.capture_ratio_pct is None


def test_none_averages_from_an_empty_group_are_handled():
    report = _build_report(None, 0, None, None)
    assert report.avg_theoretical_edge_pct is None
    assert report.avg_realistic_executable_edge_pct is None
    assert report.capture_ratio_pct is None


def test_full_capture_when_realistic_equals_theoretical():
    report = _build_report("atomic", 5, 0.5, 0.5)
    assert report.capture_ratio_pct == 100.0
