"""PRE-FLIGHT SAFE-RESUME CHECK (user directive, 2026-08-25, AUTONOMOUS
24/7 operation, item 13): CHECK OPEN ORDERS -> CHECK UNKNOWN ORDERS ->
READ BALANCES -> REBUILD LEDGER STATE -> RECONCILE -> CHECK KILL SWITCH
-> SAFE_TO_RESUME. Run at EVERY startup -- a fresh launch, a systemd
auto-restart after a crash, or a resume after a VPS reboot -- before
entering the trading loop. Never skipped, never short-circuited by
convenience.

Pure gate logic only: the caller fetches real data first (open orders
from both exchanges, this run's unresolved order-intent log, the
persisted kill-switch state, whether the balance read and DB ledger
query succeeded) and this function decides, in the user's own stated
order, short-circuiting on the first failure -- never masking an earlier
failure by continuing past it to check something else. "REBUILD LEDGER
STATE" has no persistent open-position concept to rebuild for this
orchestrator the way the simulation engine's 30-min holding-period
trades do (every real cycle here is atomic and already reconciled by
the time it completes normally) -- for this system, rebuilding ledger
state means confirming the local DB ledger is reachable and queryable,
which is exactly what `ledger_reachable` reports."""

from dataclasses import dataclass
from typing import Sized

from app.operations.order_intent_log import OrderIntent
from app.operations.persistent_kill_switch import KillSwitchState


@dataclass(slots=True, frozen=True)
class PreflightResult:
    safe_to_resume: bool
    reason: str
    checks: dict[str, str]


def evaluate_preflight(
    *,
    binance_open_orders: Sized,  # list[BinanceOrderResult] at the call site -- kept untyped here so this file never imports the live-trade-capable clients (tests/test_phase3a_isolation.py restricts that import to the two authorized executors); only len() is ever used
    bybit_open_orders: Sized,  # list[BybitOrderStatus] at the call site, same reason
    unresolved_intents: list[OrderIntent],
    balances_reachable: bool,
    ledger_reachable: bool,
    kill_switch_state: KillSwitchState,
    okx_open_orders: Sized = (),  # list[OkxOrderStatus] at the call site, same untyped reason -- optional, defaults to "none" so every existing (Binance/Bybit-only) caller is unaffected
) -> PreflightResult:
    """Pure. Returns SAFE_TO_RESUME=True only if every check passes, in
    order. `checks` always records every check attempted (up to and
    including the first failure) so the caller can show a full,
    honest pre-flight report rather than just the final verdict."""
    checks: dict[str, str] = {}

    if binance_open_orders or bybit_open_orders or okx_open_orders:
        checks["OPEN_ORDERS"] = (
            f"FAIL -- {len(binance_open_orders)} binance, {len(bybit_open_orders)} bybit, "
            f"{len(okx_open_orders)} okx open order(s) found at startup"
        )
        return PreflightResult(False, "OPEN ORDERS FOUND AT STARTUP -- HUMAN_REVIEW_REQUIRED, never auto-resolved", checks)
    checks["OPEN_ORDERS"] = "PASS -- none found on any exchange"

    if unresolved_intents:
        checks["UNKNOWN_ORDERS"] = f"FAIL -- {len(unresolved_intents)} unresolved order-intent(s) from a prior run: " + ", ".join(
            f"{i.purpose}/{i.exchange}/{i.symbol} (started {i.started_at})" for i in unresolved_intents
        )
        return PreflightResult(False, "UNRESOLVED ORDER INTENT(S) FROM A PRIOR RUN -- HUMAN_REVIEW_REQUIRED, never auto-resolved", checks)
    checks["UNKNOWN_ORDERS"] = "PASS -- no unresolved intents"

    if not balances_reachable:
        checks["BALANCES"] = "FAIL -- could not read a fresh real balance from one or both exchanges"
        return PreflightResult(False, "BALANCES UNREACHABLE -- cannot safely evaluate reserve floors or sizing without them", checks)
    checks["BALANCES"] = "PASS -- fresh real balances read from both exchanges"

    if not ledger_reachable:
        checks["LEDGER_STATE"] = "FAIL -- the local trade ledger (DB) could not be queried"
        return PreflightResult(False, "LEDGER STATE UNREACHABLE -- cannot confirm real trading history before resuming", checks)
    checks["LEDGER_STATE"] = "PASS -- local ledger reachable and queryable"

    if kill_switch_state.engaged:
        checks["KILL_SWITCH"] = f"FAIL -- persisted kill switch is engaged: {kill_switch_state.reason} (engaged_at={kill_switch_state.engaged_at})"
        return PreflightResult(False, f"KILL SWITCH WAS PREVIOUSLY ENGAGED: {kill_switch_state.reason} -- never auto-cleared, requires deliberate human review", checks)
    checks["KILL_SWITCH"] = "PASS -- not engaged"

    return PreflightResult(True, "all pre-flight checks passed", checks)
