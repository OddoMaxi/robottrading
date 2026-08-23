"""FINAL MULTI-SYMBOL LIVE PREFLIGHT (Phase 3, user directive,
2026-08-23) — READ-ONLY.

Combines the account-level checks already built in
app.execution.live_readiness_gate (trade permissions, withdrawals
disabled, kill switch, Profit Reality Ledger) with the NEW,
generalized-beyond-LUNC pieces this phase adds: the dynamic
Binance∩Bybit universe (app.execution.live_universe), MASTER's live
ranker (app.execution.live_ranker), and the resulting best current
candidate's capital pre-positioning check. No order is placed anywhere
in this module.
"""

from dataclasses import dataclass

from app.config.settings import get_settings
from app.execution.live_ranker import RankedOpportunity, rank_live_opportunities
from app.execution.live_readiness_gate import FirstLiveGateReport, build_first_live_gate_report
from app.execution.live_universe import LiveUniverse, live_universe_builder


@dataclass(slots=True)
class MultiSymbolPreflightReport:
    account_gate: FirstLiveGateReport
    universe: LiveUniverse
    dynamic_scanner_ready: bool
    dynamic_scanner_detail: str
    master_ranker_ready: bool
    master_ranker_detail: str
    qualified_opportunities: int
    best_candidate: RankedOpportunity | None
    ready_to_start: bool
    ready_reason: str


async def build_multi_symbol_preflight_report(requested_notional_per_leg_usdt: float | None = None) -> MultiSymbolPreflightReport:
    settings = get_settings()
    notional = requested_notional_per_leg_usdt if requested_notional_per_leg_usdt is not None else settings.max_notional_per_leg_usdt

    dynamic_scanner_ready = False
    dynamic_scanner_detail = "unable to build universe"
    universe: LiveUniverse
    try:
        universe = await live_universe_builder.get_universe()
        dynamic_scanner_ready = len(universe.common_symbols) > 0
        dynamic_scanner_detail = f"{len(universe.common_symbols)} common Binance∩Bybit USDT pairs discovered"
    except Exception as exc:
        from app.execution.live_universe import LiveUniverse as _LU
        import time as _time

        universe = _LU(common_symbols=[], binance_symbol_count=0, bybit_symbol_count=0, fetched_at=_time.time())
        dynamic_scanner_detail = f"error: {exc}"

    master_ranker_ready = False
    master_ranker_detail = "unable to rank opportunities"
    ranked: list[RankedOpportunity] = []
    try:
        ranked = await rank_live_opportunities(requested_notional_per_leg_usdt=notional, max_symbols=None)
        master_ranker_ready = True
        master_ranker_detail = f"{len(ranked)} direction(s) evaluated across {len(universe.common_symbols)} symbol(s)"
    except Exception as exc:
        master_ranker_detail = f"error: {exc}"

    qualified = [r for r in ranked if r.score > 0]
    best_candidate = qualified[0] if qualified else None

    reference_symbol = best_candidate.symbol.replace("/", "") if best_candidate is not None else "LUNCUSDT"
    account_gate = await build_first_live_gate_report(reference_symbol=reference_symbol)

    ready = (
        account_gate.binance_trade_api_ready
        and account_gate.bybit_trade_api_ready
        and account_gate.withdrawals_disabled
        and account_gate.leg_risk_protection_pass
        and account_gate.live_kill_switch_pass
        and account_gate.real_pnl_ledger_ready
        and dynamic_scanner_ready
        and master_ranker_ready
        and best_candidate is not None
        and best_candidate.prepositioning.executable_now
    )
    if ready:
        ready_reason = "all checks passed and at least one pre-positioned, real-fee-validated, net-positive candidate is currently executable"
    else:
        failing = []
        if not account_gate.binance_trade_api_ready:
            failing.append("BINANCE_TRADE_API")
        if not account_gate.bybit_trade_api_ready:
            failing.append("BYBIT_TRADE_API")
        if not account_gate.withdrawals_disabled:
            failing.append("WITHDRAWALS_NOT_DISABLED")
        if not dynamic_scanner_ready:
            failing.append("DYNAMIC_SCANNER")
        if not master_ranker_ready:
            failing.append("MASTER_RANKER")
        if best_candidate is None:
            failing.append("NO_QUALIFIED_CANDIDATE")
        elif not best_candidate.prepositioning.executable_now:
            failing.append("BEST_CANDIDATE_NOT_PREPOSITIONED")
        ready_reason = "not ready: " + ", ".join(failing) if failing else "not ready"

    return MultiSymbolPreflightReport(
        account_gate=account_gate,
        universe=universe,
        dynamic_scanner_ready=dynamic_scanner_ready,
        dynamic_scanner_detail=dynamic_scanner_detail,
        master_ranker_ready=master_ranker_ready,
        master_ranker_detail=master_ranker_detail,
        qualified_opportunities=len(qualified),
        best_candidate=best_candidate,
        ready_to_start=ready,
        ready_reason=ready_reason,
    )
