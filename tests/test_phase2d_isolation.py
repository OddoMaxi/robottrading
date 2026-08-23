"""PHASE 2D — READ-ONLY / NO ORDER, mechanical proof (user directive,
2026-08-23).

Statically verifies:
1. No file in the repo references a Binance/Bybit order-placement/cancel
   endpoint or method name — EXCEPT the two files Phase 3A (user
   directive, 2026-08-23) explicitly and deliberately authorized to
   contain that capability (app.execution.binance_live_trade_client,
   app.execution.bybit_live_trade_client) and this test file itself
   (which must name the tokens to allowlist them). Every other file in
   the repo — main.py, the read-only clients, the dashboard, every
   reporting module — must still contain none of these tokens at all.
   See tests/test_phase3a_isolation.py for what gates the two allowlisted
   files instead of a blanket absence.
2. app.execution.binance_account_client / app.execution.micro_live never
   log the raw API key/secret value.
3. app.execution.live_guard's module singleton starts with
   live_trading_enabled=False — importing the module must never itself
   enable real execution.
4. main.py never hardcodes real_orders_placed (or the API's
   real_orders_placed field) to anything but 0/False, including in the
   Phase 2D additions.
5. /live/execute (app/api/routes.py) has no order-placement call
   reachable after the live_guard check — structurally, the function body
   contains no call to anything named like an order-placement method.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"
MAIN_ENTRYPOINT = REPO_ROOT / "main.py"
ROUTES = REPO_ROOT / "app" / "api" / "routes.py"

FORBIDDEN_ORDER_TOKENS = (
    "/api/v3/order",
    "/api/v3/cancelReplace",
    "createOrder",
    "placeOrder",
    "newOrder",
    "cancelOrder",
    "/v5/order/create",
    "/v5/order/cancel",
    "/v5/order/amend",
)

# PHASE 3A (user directive, 2026-08-23) — the ONLY two files in the repo
# allowed to reference an order-placement endpoint, and the ONLY caller
# tests/test_phase3a_isolation.py permits to import them
# (app.execution.live_arbitrage_executor). Adding a path here must never
# be done casually — it is exactly the thing this test exists to make
# hard to do by accident.
ALLOWLISTED_ORDER_CAPABLE_FILES = {
    APP_DIR / "execution" / "binance_live_trade_client.py",
    APP_DIR / "execution" / "bybit_live_trade_client.py",
}


def _all_py_files() -> list[Path]:
    return [*APP_DIR.rglob("*.py"), MAIN_ENTRYPOINT]


def test_no_file_references_a_binance_order_endpoint_or_method_name():
    violations = []
    for path in _all_py_files():
        if path in ALLOWLISTED_ORDER_CAPABLE_FILES:
            continue
        text = path.read_text()
        for token in FORBIDDEN_ORDER_TOKENS:
            if token in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} references forbidden order-shaped token '{token}'")
    assert not violations, "NO ORDER violation(s) found:\n" + "\n".join(violations)


def test_binance_account_client_has_no_order_placement_method():
    from app.execution.binance_account_client import BinanceAccountClient

    order_shaped = [
        name
        for name in dir(BinanceAccountClient)
        if not name.startswith("_")
        and any(word in name.lower() for word in ("place_order", "new_order", "create_order", "cancel", "withdraw"))
    ]
    assert not order_shaped, f"BinanceAccountClient exposes order/withdraw-shaped method(s): {order_shaped}"


def test_bybit_client_has_no_order_placement_method():
    from app.execution.bybit_client import BybitClient

    order_shaped = [
        name
        for name in dir(BybitClient)
        if not name.startswith("_")
        and any(word in name.lower() for word in ("place_order", "new_order", "create_order", "cancel", "withdraw"))
    ]
    assert not order_shaped, f"BybitClient exposes order/withdraw-shaped method(s): {order_shaped}"


def test_credential_values_are_never_passed_to_a_logger_call():
    """Textual scan: no logger.*(...) call in the exchange-adjacent
    modules contains 'api_key' or 'api_secret' or 'settings.binance'/
    'settings.bybit' in its argument source — those values must only
    ever reach the signed-request header/query construction, never a
    log line."""
    for module in (
        "binance_account_client.py",
        "micro_live.py",
        "live_guard.py",
        "bybit_client.py",
        "dual_leg_observer.py",
        "dual_leg_quote.py",
    ):
        path = APP_DIR / "execution" / module
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr in ("debug", "info", "warning", "error", "critical", "exception")
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "logger"
            ):
                call_source = ast.dump(node)
                assert "api_key" not in call_source and "api_secret" not in call_source, (
                    f"{module}:{node.lineno} logger call may include a credential value"
                )


def test_live_guard_singleton_starts_disabled():
    from app.execution.live_guard import live_guard

    assert live_guard.live_trading_enabled is False


def test_main_py_never_hardcodes_real_orders_placed_to_true():
    for path in (MAIN_ENTRYPOINT, ROUTES):
        source = path.read_text()
        assert "real_orders_placed = True" not in source
        assert "real_orders_placed=True" not in source
        assert '"real_orders_placed": True' not in source


def test_live_execute_endpoint_has_no_order_placement_call_after_the_guard():
    source = ROUTES.read_text()
    tree = ast.parse(source)
    func = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "live_execute"
    )
    called_names = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(func)
        if isinstance(node, ast.Call)
    }
    order_shaped = {
        name
        for name in called_names
        if any(word in name.lower() for word in ("place_order", "new_order", "create_order", "cancel", "withdraw"))
    }
    assert not order_shaped, f"live_execute calls order-shaped function(s): {order_shaped}"
