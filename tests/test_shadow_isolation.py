"""SHADOW MEANS SHADOW — mechanical proof, not a comment promise (user
directive, 2026-08-22, Phase 2).

Statically parses every file in app/shadow/ plus shadow_orchestrator.py
with Python's own ast module and asserts none of them import — directly
or indirectly through a one-hop re-export — any module that can place a
real order, touch app.simulation.portfolios.VirtualPortfolio's real
balances, or reserve/resolve app.onchain.dex_paper_trader.DexCapitalPool's
real capital. A comment saying "this package never imports X" is a
promise; this test is the enforcement — it fails loudly the moment any
future edit to app/shadow adds a forbidden import, before that code
could ever run against production.
"""

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SHADOW_PACKAGE_DIR = REPO_ROOT / "app" / "shadow"
ORCHESTRATOR_ENTRYPOINT = REPO_ROOT / "shadow_orchestrator.py"

# Any module whose import would put a real execution/capital write path
# within reach — direct imports of these are forbidden anywhere in
# app/shadow or the orchestrator entrypoint.
FORBIDDEN_MODULE_PREFIXES = (
    "app.simulation",  # widened from individual submodules (PRE-PHASE-2 CORRECTIVE MAINTENANCE #2): app.shadow.positions deliberately reimplements app.simulation.position_tracker's logic rather than importing it — this blanket rule keeps that guarantee simple to state and enforce, not just the 3 submodules previously listed
    "app.onchain.dex_paper_trader",
    "main",  # the live engine entrypoint — its running dex_capital_pool/portfolios are real, mutable, shared state
)


def _shadow_source_files() -> list[Path]:
    files = sorted(SHADOW_PACKAGE_DIR.glob("*.py"))
    assert files, f"expected at least one file in {SHADOW_PACKAGE_DIR}"
    return files + [ORCHESTRATOR_ENTRYPOINT]


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


def test_shadow_package_files_exist_and_are_discoverable():
    files = _shadow_source_files()
    assert ORCHESTRATOR_ENTRYPOINT in files
    assert any(f.name == "decision.py" for f in files)
    assert any(f.name == "ledger.py" for f in files)


def test_no_shadow_file_imports_a_forbidden_execution_module():
    violations: list[str] = []
    for path in _shadow_source_files():
        imported = _imported_module_names(path.read_text())
        for module_name in imported:
            for forbidden in FORBIDDEN_MODULE_PREFIXES:
                if module_name == forbidden or module_name.startswith(forbidden + "."):
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports forbidden module '{module_name}' (matches '{forbidden}')")
    assert not violations, "SHADOW MEANS SHADOW violation(s) found:\n" + "\n".join(violations)


def test_shadow_reconstruct_only_writes_to_the_shadow_decisions_table():
    """A narrower, code-level check on top of the import check: the one
    module allowed to touch the database (reconstruct.py) must never
    construct a SimulatedTradeRecord or DexSimulatedTradeRecord (the real
    ledgers) — only read them via select(), and only ever write
    ShadowDecisionRecord / CexScanShadowDecisionRecord (PHASE 2B's own
    dedicated result table, added 2026-08-22 — still not a real ledger)."""
    source = (SHADOW_PACKAGE_DIR / "reconstruct.py").read_text()
    tree = ast.parse(source)
    constructed_record_types = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("Record")
    }
    assert constructed_record_types <= {"ShadowDecisionRecord", "CexScanShadowDecisionRecord"}, (
        f"app/shadow/reconstruct.py constructs {constructed_record_types} — only ShadowDecisionRecord/CexScanShadowDecisionRecord are allowed"
    )


def test_shadow_ledger_is_a_standalone_class_not_a_reference_to_the_real_pools():
    """Belt-and-suspenders: the shadow ledger's own module must not
    import DexCapitalPool or VirtualPortfolio at all — it's a fresh,
    independent reimplementation (see ledger.py's own docstring for why
    duplication is the deliberate, safety-driven choice here)."""
    from app.shadow.ledger import ShadowCapitalLedger

    import app.onchain.dex_paper_trader as real_dex_module
    import app.simulation.portfolios as real_cex_module

    assert ShadowCapitalLedger is not real_dex_module.DexCapitalPool
    assert not issubclass(ShadowCapitalLedger, real_cex_module.VirtualPortfolio)


def test_shadow_position_tracker_is_a_standalone_class_not_a_reference_to_the_real_one():
    """Same belt-and-suspenders check for fix 2 (PRE-PHASE-2 CORRECTIVE
    MAINTENANCE #2): app.shadow.positions.ShadowOpenPositionTracker must
    be its own class, not app.simulation.position_tracker.OpenPositionTracker
    imported under a different name."""
    from app.shadow.positions import ShadowOpenPositionTracker

    import app.simulation.position_tracker as real_position_module

    assert ShadowOpenPositionTracker is not real_position_module.OpenPositionTracker
    assert not issubclass(ShadowOpenPositionTracker, real_position_module.OpenPositionTracker)


# --- PHASE 2B — CEX Scan-Level Shadow telemetry isolation (user directive, 2026-08-22) ---
#
# The new telemetry tap in main.py's real detection_loop is the one place
# THIS session ever writes shadow-related code directly into the live
# engine process — these tests specifically prove that tap cannot affect
# OLD's own execution path.

REPOSITORY_MODULE = REPO_ROOT / "app" / "database" / "repository.py"
MAIN_ENTRYPOINT = REPO_ROOT / "main.py"


def test_save_cex_scan_event_only_constructs_the_telemetry_record():
    """app.database.repository.save_cex_scan_event (called from main.py's
    telemetry tap) must construct exactly one record type
    (CexScanEventRecord) and nothing else — it cannot, by construction,
    touch a real trade/balance table."""
    source = REPOSITORY_MODULE.read_text()
    tree = ast.parse(source)
    func_def = next(node for node in ast.walk(tree) if isinstance(node, ast.AsyncFunctionDef) and node.name == "save_cex_scan_event")
    constructed_record_types = {
        node.func.id for node in ast.walk(func_def) if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id.endswith("Record")
    }
    assert constructed_record_types == {"CexScanEventRecord"}, (
        f"save_cex_scan_event constructs {constructed_record_types} — must construct only CexScanEventRecord"
    )


def _innermost_try_containing(tree: ast.AST, target: ast.AST) -> ast.Try:
    """main.py's detection_loop nests the telemetry try/except INSIDE the
    loop's own outer try/except (CancelledError re-raise + a catch-all) —
    a plain ast.walk() BFS can surface that outer try first, which does
    legitimately bare-reraise for CancelledError and would produce a
    false positive here. The innermost (most tightly-scoped) try around
    the call is the one whose line span is smallest."""

    def _contains(container: ast.AST) -> bool:
        return any(node is target for node in ast.walk(container))

    candidates = [n for n in ast.walk(tree) if isinstance(n, ast.Try) and any(_contains(stmt) for stmt in n.body)]
    assert candidates, "no try block contains the target call at all"
    return min(candidates, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)


def test_main_py_telemetry_call_site_is_wrapped_in_its_own_try_except():
    """Structural proof that a telemetry failure cannot propagate and
    interrupt OLD's real trade processing for this or any other
    opportunity in the same scan cycle — the save_cex_scan_event call
    must live inside a try block whose except clause does not re-raise."""
    source = MAIN_ENTRYPOINT.read_text()
    tree = ast.parse(source)

    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "save_cex_scan_event"
    ]
    assert call_sites, "expected a save_cex_scan_event(...) call in main.py"

    try_node = _innermost_try_containing(tree, call_sites[0])
    for handler in try_node.handlers:
        reraises = any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(handler))
        assert not reraises, "the innermost except handler around save_cex_scan_event must not bare-reraise"


def test_main_py_telemetry_never_calls_paper_trader_simulate_or_attempt_dex_trade():
    """The telemetry tap itself (the innermost try block containing
    save_cex_scan_event) must not call any real execution function — it
    only reads already-decided values (validation.approved,
    opp.rejection_reason, position_tracker.is_open) and persists them."""
    source = MAIN_ENTRYPOINT.read_text()
    tree = ast.parse(source)
    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "save_cex_scan_event"
    ]
    assert call_sites

    try_node = _innermost_try_containing(tree, call_sites[0])
    forbidden_calls = {"simulate", "attempt_dex_trade", "open_position"}
    called_names = {
        n.func.attr if isinstance(n.func, ast.Attribute) else getattr(n.func, "id", None)
        for n in ast.walk(try_node)
        if isinstance(n, ast.Call)
    }
    violations = called_names & forbidden_calls
    assert not violations, f"telemetry try block calls forbidden execution function(s): {violations}"
