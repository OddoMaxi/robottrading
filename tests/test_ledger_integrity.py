from app.simulation.ledger_integrity import evaluate_ledger_integrity


def test_reconciled_when_live_and_db_equity_agree_and_nothing_negative():
    check = evaluate_ledger_integrity("5K", live_equity_usd=5_100.0, live_available_usd=3_000.0, db_reconstructed_equity_usd=5_100.0)
    assert check.reconciled is True
    assert check.violations == []


def test_tiny_floating_point_drift_within_tolerance_is_still_reconciled():
    check = evaluate_ledger_integrity("5K", live_equity_usd=5_100.001, live_available_usd=3_000.0, db_reconstructed_equity_usd=5_100.0)
    assert check.reconciled is True


def test_negative_available_capital_is_a_violation():
    check = evaluate_ledger_integrity("5K", live_equity_usd=5_100.0, live_available_usd=-50.0, db_reconstructed_equity_usd=5_100.0)
    assert check.reconciled is False
    assert any("available_usd is negative" in v for v in check.violations)


def test_negative_equity_is_a_violation():
    check = evaluate_ledger_integrity("5K", live_equity_usd=-10.0, live_available_usd=0.0, db_reconstructed_equity_usd=-10.0)
    assert check.reconciled is False
    assert any("equity is negative" in v for v in check.violations)


def test_live_and_db_equity_disagreeing_is_a_violation():
    """The scenario this check exists to catch: a DB write that silently
    failed, or a state-recovery inconsistency after a restart — the live
    portfolio's own balance says one thing, the trade ledger says another."""
    check = evaluate_ledger_integrity("5K", live_equity_usd=5_100.0, live_available_usd=3_000.0, db_reconstructed_equity_usd=4_950.0)
    assert check.reconciled is False
    assert any("disagrees with the DB ledger" in v for v in check.violations)


def test_multiple_violations_are_all_reported_not_just_the_first():
    check = evaluate_ledger_integrity("5K", live_equity_usd=-5.0, live_available_usd=-5.0, db_reconstructed_equity_usd=100.0)
    assert len(check.violations) == 3
