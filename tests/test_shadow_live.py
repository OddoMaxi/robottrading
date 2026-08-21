from app.reporting.shadow_live import _aggregate_signal_counts


def test_all_signals_approved_when_every_rejection_reason_is_none():
    total, approved, rejections = _aggregate_signal_counts([(None, 5)])
    assert total == 5
    assert approved == 5
    assert rejections == {}


def test_mixed_approved_and_rejected_signals_split_correctly():
    rows = [(None, 3), ("stale_data", 2), ("edge_too_low", 7)]
    total, approved, rejections = _aggregate_signal_counts(rows)
    assert total == 12
    assert approved == 3
    assert rejections == {"stale_data": 2, "edge_too_low": 7}


def test_no_rows_reports_all_zeros():
    total, approved, rejections = _aggregate_signal_counts([])
    assert total == 0
    assert approved == 0
    assert rejections == {}


def test_every_signal_rejected_reports_zero_approved():
    total, approved, rejections = _aggregate_signal_counts([("position_already_open", 4)])
    assert total == 4
    assert approved == 0
    assert rejections == {"position_already_open": 4}
