"""ALTCOIN SCANNER (user directive, 2026-08-23) — read-only, live
multi-exchange, multi-symbol cross-exchange opportunity monitoring.

Isolated from every real-money-adjacent package in this repo, the same
way app/shadow/ is isolated from V5/V5.5's real execution: nothing here
is imported by main.py, nothing here touches
app.orchestration.global_allocator, app.orchestration.control, or
app.execution.live_arbitrage_executor. It runs as its own process
(altcoin_scanner.py), its own systemd unit, and can be stopped or
deleted without affecting paper or live trading in any way.

MASTER stays in Shadow Mode for anything this package observes;
real_orders_placed is always 0 and no code path here could change that.
"""
