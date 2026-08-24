"""AUTOMATIC CROSS-EXCHANGE INVENTORY MANAGER — mechanical proof that it
stays SIMULATION/READ-ONLY ONLY (user directive, 2026-08-23: "Ne laisse
pas l'Inventory Manager convertir réellement les 60 USDT Bybit tant que
son comportement et ses limites n'ont pas été vérifiés" — and 2026-08-24's
V2 extension carries the exact same constraint: "STOP avant toute
conversion réelle").

Mirrors tests/test_scanner_isolation.py's proven ast-based pattern:
statically parses every Inventory-Manager-related module and asserts
none of them can, by construction, reach a real order-placement path.
app.scanner.fast_discovery (STAGE A) is already covered by
test_scanner_isolation.py's own app/scanner/*.py glob.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INVENTORY_MODULE_PATH = REPO_ROOT / "app" / "execution" / "inventory_manager.py"
V2_REPORT_MODULE_PATH = REPO_ROOT / "app" / "reporting" / "inventory_manager_v2_report.py"
DISCOVERY_REPORT_MODULE_PATH = REPO_ROOT / "app" / "reporting" / "full_universe_discovery_report.py"
ALL_MODULE_PATHS = (INVENTORY_MODULE_PATH, V2_REPORT_MODULE_PATH, DISCOVERY_REPORT_MODULE_PATH)

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


def _source(path: Path) -> str:
    return path.read_text()


def _tree(path: Path) -> ast.AST:
    return ast.parse(_source(path))


def test_all_modules_exist():
    for path in ALL_MODULE_PATHS:
        assert path.is_file(), f"missing {path}"


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
    violations: list[str] = []
    for path in ALL_MODULE_PATHS:
        imported = _imported_module_names(_tree(path))
        for name in imported:
            for forbidden in FORBIDDEN_MODULE_PREFIXES:
                if name == forbidden or name.startswith(forbidden + "."):
                    violations.append(f"{path.name} imports forbidden module '{name}'")
    assert not violations, f"forbidden import(s): {violations}"


def test_never_references_a_real_order_endpoint_token():
    violations: list[str] = []
    for path in ALL_MODULE_PATHS:
        source = _source(path)
        for token in FORBIDDEN_ORDER_ENDPOINT_TOKENS:
            if token in source:
                violations.append(f"{path.name} references forbidden order-endpoint token '{token}'")
    assert not violations, f"forbidden order-endpoint token(s): {violations}"


def test_never_calls_an_order_shaped_method():
    violations: list[str] = []
    for path in ALL_MODULE_PATHS:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else (func.id if isinstance(func, ast.Name) else None)
            if name in ORDER_SHAPED_CALL_NAMES:
                violations.append(f"{path.name}:{node.lineno}: calls '{name}'")
    assert not violations, f"order-shaped call(s): {violations}"


def test_no_module_defines_an_execute_or_submit_function():
    """No function in any of these modules may be named in a way that
    suggests it submits anything — recommend/score/check/build/value/
    render are fine, execute/submit/place/send are not."""
    violations: list[str] = []
    forbidden_name_fragments = ("execute", "submit", "place_", "send_order")
    for path in ALL_MODULE_PATHS:
        for node in ast.walk(_tree(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for fragment in forbidden_name_fragments:
                if fragment in node.name.lower():
                    violations.append(f"{path.name}: defines execution-shaped function '{node.name}'")
    assert not violations, f"execution-shaped function(s): {violations}"


def test_every_rebalance_recommendation_is_literally_marked_simulated():
    """simulated=True must be a literal boolean at every construction
    site, never a variable — a variable could someday be wired to a
    live-trading flag by accident; a literal cannot."""
    tree = _tree(INVENTORY_MODULE_PATH)
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
    tree = _tree(INVENTORY_MODULE_PATH)
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


def test_v2_final_report_hardcodes_not_ready_and_zero_orders():
    """READY TO ENABLE AUTOMATIC REAL INVENTORY MANAGEMENT and REAL
    INVENTORY ORDERS must be literal False/0 at the construction site in
    build_inventory_manager_v2_report — this is a structural
    authorization gate, not something any data quality should be able
    to flip (see the module's own docstring)."""
    tree = _tree(V2_REPORT_MODULE_PATH)
    build_fn = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "build_inventory_manager_v2_report"
    )
    return_calls = [
        node
        for node in ast.walk(build_fn)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "InventoryManagerV2FinalReport"
    ]
    assert len(return_calls) == 1
    kwargs = {kw.arg: kw.value for kw in return_calls[0].keywords}

    ready_value = kwargs.get("ready_to_enable_automatic_real_inventory_management")
    assert isinstance(ready_value, ast.Constant) and ready_value.value is False, (
        "ready_to_enable_automatic_real_inventory_management must be the literal False"
    )

    orders_value = kwargs.get("real_inventory_orders")
    assert isinstance(orders_value, ast.Constant) and orders_value.value == 0, "real_inventory_orders must be the literal 0"
