import pytest

from app.reporting.execution_funnel import build_execution_funnel_report


def test_percentages_are_relative_to_detected_total():
    report = build_execution_funnel_report(
        detected=1000,
        profitable_after_fees=400,
        profitable=250,
        rejected=350,
        executable=50,
        executed=40,
        closed=30,
        rejection_counts=[("fees_too_high", 200), ("stale_data", 100), ("edge_too_low", 50)],
    )
    assert report.stage("detected").pct_of_detected == 100.0
    assert report.stage("profitable_after_fees").pct_of_detected == 40.0
    assert report.stage("profitable").pct_of_detected == 25.0
    assert report.stage("rejected").pct_of_detected == 35.0
    assert report.stage("executable").pct_of_detected == 5.0
    assert report.stage("executed").pct_of_detected == 4.0
    assert report.stage("closed").pct_of_detected == 3.0


def test_rejection_reasons_are_percentages_of_total_rejected_not_detected():
    report = build_execution_funnel_report(
        detected=1000, profitable_after_fees=400, profitable=250, rejected=300, executable=50, executed=40, closed=30,
        rejection_counts=[("fees_too_high", 200), ("stale_data", 100)],
    )
    reasons = {reason: pct for reason, _, pct in report.rejection_reasons}
    assert reasons["fees_too_high"] == pytest.approx(66.7)
    assert reasons["stale_data"] == pytest.approx(33.3)


def test_zero_detected_never_divides_by_zero():
    report = build_execution_funnel_report(
        detected=0, profitable_after_fees=0, profitable=0, rejected=0, executable=0, executed=0, closed=0, rejection_counts=[]
    )
    assert all(s.pct_of_detected == 0.0 for s in report.stages)
    assert report.rejection_reasons == []


def test_executable_plus_rejected_equals_detected_when_no_reason_is_missing():
    """Sanity check on the funnel's own invariant: every detected
    opportunity is either rejected (a reason recorded) or executable
    (rejection_reason is None) — never neither, never both."""
    report = build_execution_funnel_report(
        detected=500, profitable_after_fees=200, profitable=100, rejected=450, executable=50, executed=45, closed=40,
        rejection_counts=[("fees_too_high", 450)],
    )
    assert report.stage("rejected").count + report.stage("executable").count == report.stage("detected").count


def test_stage_lookup_returns_none_for_an_unknown_name():
    report = build_execution_funnel_report(
        detected=10, profitable_after_fees=5, profitable=3, rejected=5, executable=5, executed=3, closed=2, rejection_counts=[]
    )
    assert report.stage("not_a_real_stage") is None


def test_attempt_outcomes_are_percentages_of_execution_attempts_not_detected():
    # execution_attempts is trade-attempt granularity (up to 5x detected,
    # one attempt per portfolio) — its own breakdown must be percentaged
    # against itself, not against "detected", or it would read past 100%.
    report = build_execution_funnel_report(
        detected=10,
        profitable_after_fees=8,
        profitable=6,
        rejected=2,
        executable=8,
        executed=8,
        closed=5,
        rejection_counts=[],
        execution_attempts=40,
        attempt_outcome_counts=[("simulated_executed", 30), ("no_capital_available", 10)],
    )
    outcomes = {status: pct for status, _, pct in report.attempt_outcomes}
    assert report.execution_attempts == 40
    assert outcomes["simulated_executed"] == 75.0
    assert outcomes["no_capital_available"] == 25.0


def test_zero_execution_attempts_never_divides_by_zero():
    report = build_execution_funnel_report(
        detected=5, profitable_after_fees=0, profitable=0, rejected=5, executable=0, executed=0, closed=0,
        rejection_counts=[("fees_too_high", 5)],
    )
    assert report.execution_attempts == 0
    assert report.attempt_outcomes == []
