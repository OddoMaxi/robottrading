from app.orchestration.control import MasterPaperControl


def test_master_control_starts_enabled():
    control = MasterPaperControl()
    assert control.paper_authority_enabled is True
    assert control.rollback_reason is None


def test_master_control_disable_records_reason_and_timestamp():
    control = MasterPaperControl()
    control.disable("ledger mismatch detected", now=123.0)
    assert control.paper_authority_enabled is False
    assert control.rollback_reason == "ledger mismatch detected"
    assert control.rollback_at == 123.0


def test_master_control_enable_clears_rollback_state():
    control = MasterPaperControl()
    control.disable("test", now=1.0)
    control.enable()
    assert control.paper_authority_enabled is True
    assert control.rollback_reason is None
    assert control.rollback_at is None


def test_master_control_disable_defaults_now_to_wall_clock():
    control = MasterPaperControl()
    control.disable("test")
    assert control.rollback_at is not None
