from app.reporting.global_capital import GlobalCapitalState


def _state(**overrides):
    defaults = dict(
        total_capital_usd=10_000.0,
        available_usd=7_500.0,
        reserved_cex_usd=1_000.0,
        reserved_dex_usd=1_500.0,
        total_reserved_usd=2_500.0,
        capital_utilization_pct=25.0,
        cex_total_capital_usd=5_000.0,
        cex_available_usd=4_000.0,
        dex_total_capital_usd=5_000.0,
        dex_available_usd=3_500.0,
    )
    defaults.update(overrides)
    return GlobalCapitalState(**defaults)


def test_global_capital_state_reconciled_when_identity_holds():
    # total = available + reserved: 10,000 = 7,500 + 2,500
    assert _state().reconciled is True


def test_global_capital_state_not_reconciled_when_identity_breaks():
    state = _state(available_usd=6_000.0)  # 10,000 != 6,000 + 2,500
    assert state.reconciled is False


def test_global_capital_state_reconciled_tolerates_subcent_float_noise():
    state = _state(available_usd=7_500.001)
    assert state.reconciled is True
