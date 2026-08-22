"""Phase 2 — Global Orchestration, SHADOW MODE ONLY (user directive,
2026-08-22).

SHADOW MEANS SHADOW: every module under this package is a pure
observation/simulation layer. None of them may import, call, or in any
way reach:

  - app.simulation.paper_trader (CEX real execution simulation)
  - app.simulation.portfolios (CEX real VirtualPortfolio balances)
  - app.simulation.time_stop (CEX real forced-exit accounting)
  - app.onchain.dex_paper_trader (DEX real execution simulation)
  - main (the live engine entrypoint — its running dex_capital_pool and
    portfolios instances are real, mutable, shared state)

tests/test_shadow_isolation.py enforces this mechanically (static import
analysis of every file in this package) — not just a comment promise.

This package only ever READS the real engines' already-persisted outputs
(via app.shadow.reconstruct, which queries simulated_trades /
dex_simulated_trades — the finished, historical record, not live state)
to reconstruct what "old_engine_decision" was, and computes its OWN,
entirely separate "master_decision" against its OWN, entirely separate
ShadowCapitalLedger (app.shadow.ledger) — a fresh instance holding
theoretical capital that was never real to begin with. Nothing in this
package writes to simulated_trades, dex_simulated_trades,
virtual_portfolios, or any table the real engines read from — only to
the new, dedicated shadow_decisions table.
"""
