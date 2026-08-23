"""MASTER Paper Authority control — rollback switch (Phase 2C, user
directive, 2026-08-23).

Mirrors app.risk.risk_engine.RiskEngine's own proven kill-switch pattern
exactly: one shared module-level instance so main.py's detection loops,
the invariant checker, and the FastAPI rollback endpoints
(app/api/routes.py) are all looking at the same flag. Disabling it is a
pure in-memory state change — every cutover-gated call site in main.py
checks `master_control.paper_authority_enabled` before doing anything
MASTER-specific, and falls back to its EXACT pre-Phase-2C behavior when
it's False. No data reconstruction, no restart, no migration — the
rollback requirement is satisfied by construction, not by a recovery
procedure.
"""

import time


class MasterPaperControl:
    def __init__(self) -> None:
        self._enabled = True
        self._rollback_reason: str | None = None
        self._rollback_at: float | None = None

    def disable(self, reason: str, now: float | None = None) -> None:
        self._enabled = False
        self._rollback_reason = reason
        self._rollback_at = now if now is not None else time.time()

    def enable(self) -> None:
        self._enabled = True
        self._rollback_reason = None
        self._rollback_at = None

    @property
    def paper_authority_enabled(self) -> bool:
        return self._enabled

    @property
    def rollback_reason(self) -> str | None:
        return self._rollback_reason

    @property
    def rollback_at(self) -> float | None:
        return self._rollback_at


# Continuous Execution spec's own convention (see app.risk.risk_engine's
# identical docstring) — one shared instance so every module that needs
# it sees the same state, without a circular import between main.py and
# app/api/routes.py.
master_control = MasterPaperControl()
