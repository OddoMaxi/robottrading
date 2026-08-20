"""Risk Engine (section 23) — gates every simulated (and, later, real) execution."""

from app.opportunity.models import Opportunity
from app.risk.limits import RiskLimits


class KillSwitchActive(Exception):
    """Raised when execution is attempted while the kill switch is engaged."""


class RiskEngine:
    def __init__(self, limits: RiskLimits = RiskLimits()) -> None:
        self.limits = limits
        self._kill_switch_engaged = False

    def engage_kill_switch(self, reason: str) -> None:
        self._kill_switch_engaged = True
        self._kill_switch_reason = reason

    def disengage_kill_switch(self) -> None:
        self._kill_switch_engaged = False
        self._kill_switch_reason = None

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    def check(self, opportunity: Opportunity, capital_usd: float, current_latency_ms: float) -> list[str]:
        """Return a list of violated limit names. Empty list means the trade may proceed."""
        if self._kill_switch_engaged:
            raise KillSwitchActive(self._kill_switch_reason or "kill switch engaged")

        violations: list[str] = []
        if capital_usd > self.limits.max_capital_per_trade_usd:
            violations.append("max_capital_per_trade_usd")
        if current_latency_ms > self.limits.max_latency_ms:
            violations.append("max_latency_ms")
        return violations


# Continuous Execution spec, section 61 — one shared instance so the
# FastAPI kill-switch endpoints (app/api/routes.py) and the detection loop
# (main.py) are looking at the same switch, even though they live in
# different modules to avoid a circular import between them.
risk_engine = RiskEngine()
