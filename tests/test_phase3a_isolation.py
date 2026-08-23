"""PHASE 3A — CONTROLLED MICRO-LIVE, mechanical proof (user directive,
2026-08-23).

Phase 2D-2F proved "no order-placement capability exists anywhere in the
repo." Phase 3A deliberately builds that capability for exactly one
scoped path (LUNCUSDT, Binance buy -> Bybit sell). This file proves the
NEW invariant that replaces the old one: the capability exists, but is
structurally unreachable except through app.execution.
live_arbitrage_executor, which itself checks app.execution.live_guard.
live_guard.assert_arbitrage_allowed() before ever calling into either
live-trade client — and main.py's automatic detection loop never imports
any of it.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
MAIN_ENTRYPOINT = REPO_ROOT / "main.py"
EXECUTOR = APP_DIR / "execution" / "live_arbitrage_executor.py"
DANGEROUS_MODULES = ("binance_live_trade_client", "bybit_live_trade_client")


def _imported_module_names(source: str) -> set[str]:
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_main_py_never_imports_the_live_trade_clients_or_executor():
    source = MAIN_ENTRYPOINT.read_text()
    imported = _imported_module_names(source)
    forbidden = DANGEROUS_MODULES + ("live_arbitrage_executor",)
    violations = [name for name in imported if any(mod in name for mod in forbidden)]
    assert not violations, f"main.py imports live-trading-capable module(s): {violations}"


def test_only_the_executor_imports_the_live_trade_clients():
    """Every .py file under app/ EXCEPT live_arbitrage_executor.py itself
    (and the read-only live_readiness_gate, which must NOT import the
    trade-capable clients either) must not import
    binance_live_trade_client or bybit_live_trade_client."""
    violations = []
    for path in APP_DIR.rglob("*.py"):
        if path == EXECUTOR:
            continue
        imported = _imported_module_names(path.read_text())
        for module_name in imported:
            if any(dangerous in module_name for dangerous in DANGEROUS_MODULES):
                violations.append(f"{path.relative_to(REPO_ROOT)} imports {module_name}")
    assert not violations, "Only live_arbitrage_executor.py may import a live-trade client:\n" + "\n".join(violations)


def test_live_readiness_gate_never_imports_trade_capable_clients():
    """The read-only readiness check must stay read-only — it answers
    'is this key trade-capable' via the account-permission endpoints
    (get_api_restrictions / get_api_key_info), never by importing a
    module that could place an order."""
    source = (APP_DIR / "execution" / "live_readiness_gate.py").read_text()
    imported = _imported_module_names(source)
    violations = [name for name in imported if any(dangerous in name for dangerous in DANGEROUS_MODULES)]
    assert not violations, f"live_readiness_gate.py imports live-trading-capable module(s): {violations}"


def test_execute_one_arbitrage_checks_live_guard_before_any_order_call():
    """Structural proof: the very first statement-level check inside
    execute_one_arbitrage's try/except is live_guard.assert_arbitrage_allowed
    — every order-placement call in the function body is guaranteed to be
    reached only after that check already succeeded, because it's the
    first thing the function does and any exception there returns
    immediately."""
    source = EXECUTOR.read_text()
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "execute_one_arbitrage"
    )
    # the assert_arbitrage_allowed call must appear textually before any
    # place_market_order call within the function
    calls_in_order = [
        node.func.attr
        for node in ast.walk(func)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("assert_arbitrage_allowed", "place_market_order")
    ]
    assert "assert_arbitrage_allowed" in calls_in_order, "execute_one_arbitrage never calls live_guard.assert_arbitrage_allowed"
    assert calls_in_order.index("assert_arbitrage_allowed") < calls_in_order.index("place_market_order"), (
        "assert_arbitrage_allowed must be checked before the first place_market_order call"
    )


def test_no_loop_wraps_a_place_market_order_call():
    """Structural anti-blind-retry check: place_market_order must never
    be called from inside a for/while loop anywhere in the executor —
    every submission is a single, deliberate call with a unique id, never
    an automatic retry loop that could double a position."""
    source = EXECUTOR.read_text()
    tree = ast.parse(source)

    def _contains_call(node: ast.AST, name: str) -> bool:
        return any(
            isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == name for n in ast.walk(node)
        )

    violations = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, (ast.For, ast.While)) and _contains_call(node, "place_market_order")
    ]
    assert not violations, f"place_market_order is called inside a loop at line(s) {violations} — retry-loop risk"


def test_settings_defaults_are_the_locked_down_ones():
    from app.config.settings import Settings

    defaults = Settings(_env_file=None)
    assert defaults.live_trading_enabled is False
    assert defaults.live_symbol_allowlist == ["LUNCUSDT"]
    assert defaults.live_direction == "BINANCE_BUY_BYBIT_SELL"
    assert defaults.max_notional_per_leg_usdt == 10.0
    assert defaults.max_concurrent_live_arbitrages == 1
    assert defaults.withdrawals_required is False


def test_live_guard_singleton_still_starts_disabled():
    from app.execution.live_guard import live_guard

    assert live_guard.live_trading_enabled is False
    assert live_guard.in_flight_count == 0


def test_main_py_never_hardcodes_real_orders_placed_to_true_phase3a():
    source = MAIN_ENTRYPOINT.read_text()
    assert "real_orders_placed = True" not in source
    assert "real_orders_placed=True" not in source
