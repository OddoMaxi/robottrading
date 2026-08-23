from dataclasses import dataclass

from app.reporting.live_validation_score import (
    MIN_COMPLETED_TRADES_TO_JUDGE,
    compute_validation_score,
)


@dataclass
class _FakeRecord:
    actual_net_pnl_usd: float
    prediction_error_usd: float


def _records(pnls, errors=None):
    errors = errors or [0.0] * len(pnls)
    return [_FakeRecord(actual_net_pnl_usd=p, prediction_error_usd=e) for p, e in zip(pnls, errors)]


def test_no_completed_trades_is_not_eligible():
    report = compute_validation_score(total_attempts=3, completed=[], neutralization_failures=0, unknown_leg_outcomes=0)
    assert report.eligible_for_size_increase is False
    assert report.completed_trades == 0


def test_too_few_trades_is_not_eligible_even_if_all_profitable():
    records = _records([0.05] * 3)  # below MIN_COMPLETED_TRADES_TO_JUDGE
    report = compute_validation_score(total_attempts=3, completed=records, neutralization_failures=0, unknown_leg_outcomes=0)
    assert report.eligible_for_size_increase is False
    assert "completed trades" in report.eligibility_reason


def test_low_profitable_rate_is_not_eligible():
    records = _records([0.05] * 5 + [-0.05] * 10)  # 33% profitable, 15 trades
    report = compute_validation_score(total_attempts=15, completed=records, neutralization_failures=0, unknown_leg_outcomes=0)
    assert report.eligible_for_size_increase is False
    assert "profitable rate" in report.eligibility_reason


def test_high_prediction_error_is_not_eligible():
    records = _records([0.05] * MIN_COMPLETED_TRADES_TO_JUDGE, errors=[0.5] * MIN_COMPLETED_TRADES_TO_JUDGE)
    report = compute_validation_score(total_attempts=10, completed=records, neutralization_failures=0, unknown_leg_outcomes=0)
    assert report.eligible_for_size_increase is False
    assert "prediction error" in report.eligibility_reason


def test_neutralization_failure_blocks_eligibility_even_with_good_stats():
    records = _records([0.05] * MIN_COMPLETED_TRADES_TO_JUDGE, errors=[0.001] * MIN_COMPLETED_TRADES_TO_JUDGE)
    report = compute_validation_score(total_attempts=10, completed=records, neutralization_failures=1, unknown_leg_outcomes=0)
    assert report.eligible_for_size_increase is False
    assert "neutralization" in report.eligibility_reason


def test_eligible_when_all_thresholds_met():
    records = _records([0.05] * MIN_COMPLETED_TRADES_TO_JUDGE, errors=[0.001] * MIN_COMPLETED_TRADES_TO_JUDGE)
    report = compute_validation_score(total_attempts=10, completed=records, neutralization_failures=0, unknown_leg_outcomes=0)
    assert report.eligible_for_size_increase is True
    assert report.profitable_rate_pct == 100.0
