"""Live execution guard (Phase 2D, items 6-7, user directive, 2026-08-23).

LIVE_TRADING_ENABLED = false is the hard default (app.config.settings) and
this module is the ONLY place that is allowed to decide "yes, a real order
may proceed" — every future execution path must route through
assert_execution_allowed() first and treat LiveExecutionRefused as final.

Two independently-checked constants, on purpose (defense in depth):
- settings.micro_live_cap_usdt: applied by app.execution.reality_quote at
  SIZING time (item 6 — never silently reuse the $10,000 PAPER_CAPITAL).
- settings.max_live_capital_usdt: applied HERE, at the execution-attempt
  boundary itself (item 7). A bug that let an oversized request through
  sizing would still be caught here, and vice versa.

Nothing in this codebase currently calls execute() with real order
placement wired up — this module exists so that when that step is
explicitly authorized in the future, the refusal is structural, not a
matter of remembering to check a flag.
"""

import time


class LiveExecutionRefused(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class LiveTradingGuard:
    def __init__(
        self,
        live_trading_enabled: bool,
        max_live_capital_usdt: float,
        symbol_allowlist: list[str] | None = None,
        allowed_directions: list[str] | None = None,
        max_notional_per_leg_usdt: float | None = None,
        max_concurrent_arbitrages: int = 1,
    ) -> None:
        self._live_trading_enabled = live_trading_enabled
        self._max_live_capital_usdt = max_live_capital_usdt
        # PHASE 3 (user directive, 2026-08-23): an EMPTY allowlist means
        # "unrestricted — any symbol in the dynamically-discovered
        # universe is eligible," not "nothing is." A non-empty list still
        # restricts to exactly those symbols, for an operator who wants
        # to narrow scope back down deliberately.
        self._symbol_allowlist = set(symbol_allowlist or [])
        self._allowed_directions = set(allowed_directions or [])
        self._max_notional_per_leg_usdt = max_notional_per_leg_usdt
        self._max_concurrent_arbitrages = max_concurrent_arbitrages
        self._in_flight_count = 0
        self._kill_switch_engaged = False
        self._kill_switch_reason: str | None = None
        self._kill_switch_at: float | None = None

    @property
    def live_trading_enabled(self) -> bool:
        return self._live_trading_enabled

    @property
    def max_live_capital_usdt(self) -> float:
        return self._max_live_capital_usdt

    @property
    def kill_switch_engaged(self) -> bool:
        return self._kill_switch_engaged

    @property
    def kill_switch_reason(self) -> str | None:
        return self._kill_switch_reason

    def engage_kill_switch(self, reason: str, now: float | None = None) -> None:
        self._kill_switch_engaged = True
        self._kill_switch_reason = reason
        self._kill_switch_at = now if now is not None else time.time()

    def disengage_kill_switch(self) -> None:
        self._kill_switch_engaged = False
        self._kill_switch_reason = None
        self._kill_switch_at = None

    def assert_execution_allowed(self, requested_usdt: float) -> None:
        """Raises LiveExecutionRefused for any reason at all to proceed —
        callers must never catch this and continue, only report it."""
        if self._kill_switch_engaged:
            raise LiveExecutionRefused(f"live kill switch engaged: {self._kill_switch_reason}")
        if not self._live_trading_enabled:
            raise LiveExecutionRefused("live_trading_enabled is False")
        if requested_usdt <= 0:
            raise LiveExecutionRefused(f"requested_usdt must be positive, got {requested_usdt}")
        if requested_usdt > self._max_live_capital_usdt:
            raise LiveExecutionRefused(
                f"requested {requested_usdt} USDT exceeds max_live_capital_usdt cap of {self._max_live_capital_usdt} USDT"
            )

    def assert_arbitrage_allowed(self, symbol: str, direction: str, notional_per_leg_usdt: float) -> None:
        """PHASE 3A (user directive, 2026-08-23) — the specific gate
        app.execution.live_arbitrage_executor must pass before EVERY
        real dual-leg attempt (not just once at startup): kill switch,
        live_trading_enabled, symbol allowlist, configured direction,
        per-leg notional cap, and the concurrent-arbitrage limit. Raises
        LiveExecutionRefused for any reason at all to proceed — callers
        must never catch this and continue, only report it and stop."""
        if self._kill_switch_engaged:
            raise LiveExecutionRefused(f"live kill switch engaged: {self._kill_switch_reason}")
        if not self._live_trading_enabled:
            raise LiveExecutionRefused("live_trading_enabled is False")
        if self._symbol_allowlist and symbol not in self._symbol_allowlist:
            raise LiveExecutionRefused(f"symbol {symbol} not in live_symbol_allowlist {sorted(self._symbol_allowlist)}")
        if self._allowed_directions and direction not in self._allowed_directions:
            raise LiveExecutionRefused(f"direction {direction} not in allowed_directions {sorted(self._allowed_directions)}")
        if notional_per_leg_usdt <= 0:
            raise LiveExecutionRefused(f"notional_per_leg_usdt must be positive, got {notional_per_leg_usdt}")
        if self._max_notional_per_leg_usdt is not None and notional_per_leg_usdt > self._max_notional_per_leg_usdt:
            raise LiveExecutionRefused(
                f"requested {notional_per_leg_usdt} USDT per leg exceeds max_notional_per_leg_usdt cap of {self._max_notional_per_leg_usdt} USDT"
            )
        if self._in_flight_count >= self._max_concurrent_arbitrages:
            raise LiveExecutionRefused(
                f"max_concurrent_live_arbitrages ({self._max_concurrent_arbitrages}) already in flight"
            )

    def register_arbitrage_start(self) -> None:
        self._in_flight_count += 1

    def register_arbitrage_end(self) -> None:
        self._in_flight_count = max(0, self._in_flight_count - 1)

    @property
    def in_flight_count(self) -> int:
        return self._in_flight_count

    def status(self) -> dict:
        return {
            "live_trading_enabled": self._live_trading_enabled,
            "max_live_capital_usdt": self._max_live_capital_usdt,
            "symbol_allowlist": sorted(self._symbol_allowlist),
            "allowed_directions": sorted(self._allowed_directions),
            "max_notional_per_leg_usdt": self._max_notional_per_leg_usdt,
            "max_concurrent_arbitrages": self._max_concurrent_arbitrages,
            "in_flight_count": self._in_flight_count,
            "kill_switch_engaged": self._kill_switch_engaged,
            "kill_switch_reason": self._kill_switch_reason,
            "kill_switch_at": self._kill_switch_at,
        }


def _build_default_live_guard() -> LiveTradingGuard:
    from app.config.settings import get_settings

    settings = get_settings()
    return LiveTradingGuard(
        live_trading_enabled=settings.live_trading_enabled,
        max_live_capital_usdt=settings.max_live_capital_usdt,
        symbol_allowlist=settings.live_symbol_allowlist,
        allowed_directions=settings.live_allowed_directions,
        max_notional_per_leg_usdt=settings.max_notional_per_leg_usdt,
        max_concurrent_arbitrages=settings.max_concurrent_live_arbitrages,
    )


# Module-level singleton — same convention as app.risk.risk_engine and
# app.orchestration.control.master_control, so main.py, the API routes,
# and the dashboard all observe one shared kill-switch state.
live_guard = _build_default_live_guard()
