import pytest

from app.config.constants import Strategy
from app.opportunity.models import Opportunity
from app.risk.limits import RiskLimits
from app.risk.risk_engine import KillSwitchActive, RiskEngine


def make_opp() -> Opportunity:
    return Opportunity(strategy=Strategy.CROSS_EXCHANGE, symbol="BTC/USDT", legs=[], gross_spread_pct=0.5)


def test_check_passes_with_no_violations_under_normal_conditions():
    engine = RiskEngine(RiskLimits(max_capital_per_trade_usd=5_000, max_latency_ms=500))
    assert engine.check(make_opp(), capital_usd=1_000, current_latency_ms=50) == []


def test_check_flags_capital_over_the_limit():
    engine = RiskEngine(RiskLimits(max_capital_per_trade_usd=5_000, max_latency_ms=500))
    violations = engine.check(make_opp(), capital_usd=10_000, current_latency_ms=50)
    assert "max_capital_per_trade_usd" in violations


def test_check_flags_latency_over_the_limit():
    engine = RiskEngine(RiskLimits(max_capital_per_trade_usd=5_000, max_latency_ms=500))
    violations = engine.check(make_opp(), capital_usd=1_000, current_latency_ms=900)
    assert "max_latency_ms" in violations


def test_kill_switch_starts_disengaged():
    engine = RiskEngine()
    assert engine.kill_switch_engaged is False


def test_engaging_the_kill_switch_blocks_every_check():
    engine = RiskEngine()
    engine.engage_kill_switch("manual stop")
    assert engine.kill_switch_engaged is True
    with pytest.raises(KillSwitchActive):
        engine.check(make_opp(), capital_usd=1, current_latency_ms=1)


def test_disengaging_the_kill_switch_restores_normal_checks():
    engine = RiskEngine()
    engine.engage_kill_switch("manual stop")
    engine.disengage_kill_switch()
    assert engine.kill_switch_engaged is False
    assert engine.check(make_opp(), capital_usd=1, current_latency_ms=1) == []
