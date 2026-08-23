"""AUTOMATIC CROSS-EXCHANGE INVENTORY MANAGER — mechanical proof that it
stays SIMULATION/READ-ONLY ONLY (user directive, 2026-08-23: "Ne laisse
pas l'Inventory Manager convertir réellement les 60 USDT Bybit tant que
son comportement et ses limites n'ont pas été vérifiés").

Mirrors tests/test_scanner_isolation.py's proven ast-based pattern:
statically parses app/execution/inventory_manager.py and asserts it
cannot, by construction, reach a real order-placement path.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
MODULE_PATH = REPO_ROOT / "app" / "execution" / "inventory_manager.py"

FORBIDDEN_MODULE_PREFIXES = (
    "app.orchestration",
    "app.execution.binance_live_trade_client",
    "app.execution.bybit_live_trade_client",
    "app.execution.live_arbitrage_executor",
    "app.simulation",
    "app.onchain.dex_paper_trader",
    "main",
)

FORBIDDEN_ORDER_ENDPOINT_TOKENS = ("/api/v3/order", "/v5/order/create")

ORDER_SHAPED_CALL_NAMES = ("place_market_order", "place_order", "new_order", "create_order", "cancel_order", "withdraw")


def _source() -> str:
    return MODULE_PATH.read_text()


def _tree() -> ast.AST:
    return ast.parse(_source())


def test_module_exists():
    assert MODULE_PATH.is_file()


def _imported_module_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_never_imports_a_forbidden_execution_module():
    imported = _imported_module_names(_tree())
    violations = [
        name
        for name in imported
        for forbidden in FORBIDDEN_MODULE_PREFIXES
        if name == forbidden or name.startswith(forbidden + ".")
    ]
    assert not violations, f"inventory_manager.py imports forbidden module(s): {violations}"


def test_never_references_a_real_order_endpoint_token():
    source = _source()
    violations = [token for token in FORBIDDEN_ORDER_ENDPOINT_TOKENS if token in source]
    assert not violations, f"inventory_manager.py references forbidden order-endpoint token(s): {violations}"


def test_never_calls_an_order_shaped_method():
    tree = _tree()
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
        if name in ORDER_SHAPED_CALL_NAMES:
            violations.append(f"line {node.lineno}: calls '{name}'")
    assert not violations, f"inventory_manager.py calls order-shaped method(s): {violations}"


def test_every_rebalance_recommendation_is_literally_marked_simulated():
    """simulated=True must be a literal boolean at every construction
    site, never a variable — a variable could someday be wired to a
    live-trading flag by accident; a literal cannot."""
    tree = _tree()
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "RebalanceRecommendation"
    ]
    assert call_sites, "expected at least one RebalanceRecommendation(...) construction site"
    for node in call_sites:
        simulated_kwargs = [kw for kw in node.keywords if kw.arg == "simulated"]
        assert len(simulated_kwargs) == 1, f"line {node.lineno}: RebalanceRecommendation must set simulated= exactly once"
        value = simulated_kwargs[0].value
        assert isinstance(value, ast.Constant) and value.value is True, (
            f"line {node.lineno}: simulated= must be the literal True, got {ast.dump(value)}"
        )


def test_report_dataclass_hardcodes_simulation_only_true():
    """build_inventory_report's return statement must set
    simulation_only=True literally, same reasoning as above."""
    tree = _tree()
    build_fn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_inventory_report"
    )
    return_calls = [
        node
        for node in ast.walk(build_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "InventoryManagerReport"
    ]
    assert len(return_calls) == 1
    kwargs = {kw.arg: kw.value for kw in return_calls[0].keywords}
    assert "simulation_only" in kwargs
    value = kwargs["simulation_only"]
    assert isinstance(value, ast.Constant) and value.value is True


def test_module_never_defines_an_execute_or_submit_function():
    """No function in this module may be named in a way that suggests it
    submits anything — recommend/score/check/build/value are fine,
    execute/submit/place/send are not."""
    tree = _tree()
    forbidden_name_fragments = ("execute", "submit", "place_", "send_order")
    violations = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        for fragment in forbidden_name_fragments
        if fragment in node.name.lower()
    ]
    assert not violations, f"inventory_manager.py defines execution-shaped function(s): {violations}"
