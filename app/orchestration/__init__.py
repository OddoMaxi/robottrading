"""Phase 2C — Controlled Paper Cutover (user directive, 2026-08-23).

PAPER TRADING ONLY. Unlike app.shadow (pure observation, never influences
anything the real engines do), this package's decisions DO gate whether
the existing CEX/DEX paper executors — app.simulation.paper_trader.
PaperTrader.simulate, app.onchain.dex_paper_trader.attempt_dex_trade,
BOTH left completely unmodified — get called at all, and with how much
capital, for the empirically-validated strategies only (cross_exchange,
atomic, dex_triangular, dex_multihop, dex_cross — app.orchestration.
global_allocator.CUTOVER_STRATEGIES).

Still 100% simulated: never calls a real executor, never needs a real
API key, real_orders_placed stays false throughout. What changes from
Phase 2B is that MASTER's decision now actually determines how much of
the $10,000 unified paper pool (CEX "5K" reference portfolio + DEX
$5,000 pool) a given opportunity may draw from — capped by handing the
existing executors a CAPPED COPY of the Opportunity object
(dataclasses.replace(opp, capital_usd=granted_amount)), never by
rewriting their internal simulation logic.

Rollback (app.orchestration.control.master_control) is a pure in-memory
flag, mirroring app.risk.risk_engine.risk_engine's own proven kill-switch
pattern — disabling it makes every cutover-gated call site fall back
EXACTLY to its pre-Phase-2C behavior, instantly, with no data
reconstruction required.
"""
