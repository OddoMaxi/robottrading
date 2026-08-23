"""ALTCOIN SCANNER — MASTER STAYS IN SHADOW MODE, mechanical proof (user
directive, 2026-08-23).

Statically parses every file in app/scanner/ plus altcoin_scanner.py and
asserts none of them import anything that could place a real order,
touch paper capital, or reach MASTER's execution authority — mirroring
tests/test_shadow_isolation.py's proven pattern.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCANNER_PACKAGE_DIR = REPO_ROOT / "app" / "scanner"
SCANNER_ENTRYPOINT = REPO_ROOT / "altcoin_scanner.py"

FORBIDDEN_MODULE_PREFIXES = (
    "app.orchestration",
    "app.execution.binance_live_trade_client",
    "app.execution.bybit_live_trade_client",
    "app.execution.live_arbitrage_executor",
    "app.simulation",
    "app.onchain.dex_paper_trader",
    "main",
)


def _scanner_source_files() -> list[Path]:
    files = sorted(SCANNER_PACKAGE_DIR.glob("*.py"))
    assert files, f"expected at least one file in {SCANNER_PACKAGE_DIR}"
    return files + [SCANNER_ENTRYPOINT]


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


def test_scanner_package_files_exist_and_are_discoverable():
    files = _scanner_source_files()
    assert SCANNER_ENTRYPOINT in files
    assert any(f.name == "cross_exchange_scanner.py" for f in files)


def test_no_scanner_file_imports_a_forbidden_execution_module():
    violations: list[str] = []
    for path in _scanner_source_files():
        imported = _imported_module_names(path.read_text())
        for module_name in imported:
            for forbidden in FORBIDDEN_MODULE_PREFIXES:
                if module_name == forbidden or module_name.startswith(forbidden + "."):
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports forbidden module '{module_name}' (matches '{forbidden}')")
    assert not violations, "MASTER STAYS IN SHADOW MODE violation(s) found:\n" + "\n".join(violations)


def test_main_py_never_imports_the_scanner():
    source = (REPO_ROOT / "main.py").read_text()
    imported = _imported_module_names(source)
    violations = [name for name in imported if "scanner" in name or "altcoin" in name]
    assert not violations, f"main.py imports scanner module(s): {violations}"


def test_save_altcoin_scan_observation_only_constructs_its_own_record_type():
    """app.database.repository.save_altcoin_scan_observation must
    construct exactly one record type — it cannot, by construction,
    touch a real trade/balance/capital table."""
    source = (REPO_ROOT / "app" / "database" / "repository.py").read_text()
    tree = ast.parse(source)
    func_def = next(
        node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "save_altcoin_scan_observation"
    )
    constructed_record_types = {
        node.func.id for node in ast.walk(func_def) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("Record")
    }
    assert constructed_record_types == {"AltcoinScanObservationRecord"}, (
        f"save_altcoin_scan_observation constructs {constructed_record_types} — only AltcoinScanObservationRecord is allowed"
    )


def test_okx_public_client_has_no_order_placement_method():
    from app.scanner.okx_public_client import OkxPublicClient

    order_shaped = [
        name
        for name in dir(OkxPublicClient)
        if not name.startswith("_")
        and any(word in name.lower() for word in ("place_order", "new_order", "create_order", "cancel", "withdraw"))
    ]
    assert not order_shaped, f"OkxPublicClient exposes order/withdraw-shaped method(s): {order_shaped}"


def test_no_file_references_an_okx_order_endpoint():
    forbidden_tokens = ("/api/v5/trade/order", "/api/v5/trade/cancel-order", "/api/v5/trade/batch-orders")
    violations = []
    for path in _scanner_source_files():
        text = path.read_text()
        for token in forbidden_tokens:
            if token in text:
                violations.append(f"{path.relative_to(REPO_ROOT)} references forbidden token '{token}'")
    assert not violations, "NO ORDER violation(s) found:\n" + "\n".join(violations)
