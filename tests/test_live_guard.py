import pytest

from app.execution.live_guard import LiveExecutionRefused, LiveTradingGuard, live_guard


def test_default_singleton_starts_with_live_trading_disabled():
    """The single most important assertion in Phase 2D: importing this
    module must never itself enable real execution."""
    assert live_guard.live_trading_enabled is False


def test_guard_refuses_when_live_trading_disabled():
    guard = LiveTradingGuard(live_trading_enabled=False, max_live_capital_usdt=10.0)
    with pytest.raises(LiveExecutionRefused, match="live_trading_enabled is False"):
        guard.assert_execution_allowed(5.0)


def test_guard_allows_when_enabled_and_within_cap():
    guard = LiveTradingGuard(live_trading_enabled=True, max_live_capital_usdt=10.0)
    guard.assert_execution_allowed(10.0)  # must not raise


def test_guard_refuses_above_max_live_capital_even_when_enabled():
    guard = LiveTradingGuard(live_trading_enabled=True, max_live_capital_usdt=10.0)
    with pytest.raises(LiveExecutionRefused, match="exceeds max_live_capital_usdt"):
        guard.assert_execution_allowed(10.01)


def test_guard_refuses_non_positive_amounts():
    guard = LiveTradingGuard(live_trading_enabled=True, max_live_capital_usdt=10.0)
    with pytest.raises(LiveExecutionRefused):
        guard.assert_execution_allowed(0.0)
    with pytest.raises(LiveExecutionRefused):
        guard.assert_execution_allowed(-5.0)


def test_kill_switch_refuses_even_when_live_trading_enabled():
    guard = LiveTradingGuard(live_trading_enabled=True, max_live_capital_usdt=10.0)
    guard.engage_kill_switch("operator judgment call", now=1.0)
    with pytest.raises(LiveExecutionRefused, match="kill switch"):
        guard.assert_execution_allowed(5.0)


def test_kill_switch_disengage_restores_normal_gating():
    guard = LiveTradingGuard(live_trading_enabled=True, max_live_capital_usdt=10.0)
    guard.engage_kill_switch("test")
    guard.disengage_kill_switch()
    guard.assert_execution_allowed(5.0)  # must not raise
    assert guard.kill_switch_engaged is False
    assert guard.kill_switch_reason is None


def test_status_never_includes_credentials():
    guard = LiveTradingGuard(live_trading_enabled=False, max_live_capital_usdt=10.0)
    status = guard.status()
    for key in status:
        assert "key" not in key.lower() and "secret" not in key.lower()
