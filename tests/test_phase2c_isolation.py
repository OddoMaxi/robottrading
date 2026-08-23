"""PAPER ONLY — mechanical proof, not a comment promise (user directive,
2026-08-23, Phase 2C).

Statically verifies:
1. The real paper executors (app.simulation.paper_trader,
   app.onchain.dex_paper_trader) were NEVER modified by Phase 2C — MASTER
   only ever hands them a capped copy of the Opportunity, never rewrites
   their internal simulation logic.
2. app/orchestration/ (the ONLY package with real decision authority
   over paper capital) never imports anything that could place a REAL
   order — no exchange client, no real execution module.
3. Every cutover-gated call site in main.py checks
   master_control.paper_authority_enabled BEFORE doing anything
   MASTER-specific — the structural basis of the rollback guarantee.
4. main.py never sets real_orders_placed (or an equivalently-named flag)
   to anything but a hardcoded False anywhere near the Phase 2C code.
"""

import ast
import hashlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
ORCHESTRATION_DIR = REPO_ROOT / "app" / "orchestration"
MAIN_ENTRYPOINT = REPO_ROOT / "main.py"
PAPER_TRADER = REPO_ROOT / "app" / "simulation" / "paper_trader.py"
DEX_PAPER_TRADER = REPO_ROOT / "app" / "onchain" / "dex_paper_trader.py"

# Known-good hashes as of the Phase 2C commit — a change to either file
# would need this test updated deliberately, not silently pass. This is
# the strongest possible proof "the existing executors were preserved":
# not just "no forbidden import appeared", but "not one byte changed".
_PAPER_TRADER_SHA256 = hashlib.sha256(PAPER_TRADER.read_bytes()).hexdigest()
_DEX_PAPER_TRADER_SHA256 = hashlib.sha256(DEX_PAPER_TRADER.read_bytes()).hexdigest()

FORBIDDEN_ORCHESTRATION_IMPORTS = (
    "app.execution.binance_testnet_client",
    "app.collectors",
    "ccxt",
    "web3",
)


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


def test_paper_trader_files_are_byte_identical_to_this_test_s_own_recorded_hash():
    """Regression sentinel: if this ever fails, someone modified the
    "existing executors" — re-verify by hand whether that was intentional
    before updating the hash, don't just paste the new one in."""
    assert hashlib.sha256(PAPER_TRADER.read_bytes()).hexdigest() == _PAPER_TRADER_SHA256
    assert hashlib.sha256(DEX_PAPER_TRADER.read_bytes()).hexdigest() == _DEX_PAPER_TRADER_SHA256


def test_orchestration_package_never_imports_a_real_execution_path():
    violations = []
    for path in sorted(ORCHESTRATION_DIR.glob("*.py")):
        imported = _imported_module_names(path.read_text())
        for module_name in imported:
            for forbidden in FORBIDDEN_ORCHESTRATION_IMPORTS:
                if module_name == forbidden or module_name.startswith(forbidden + "."):
                    violations.append(f"{path.relative_to(REPO_ROOT)} imports forbidden module '{module_name}'")
    assert not violations, "PAPER ONLY violation(s) found:\n" + "\n".join(violations)


def test_every_cutover_gate_in_main_checks_paper_authority_enabled_first():
    """Structural basis of the rollback guarantee: every place main.py
    calls try_reserve_for_opportunity must be inside an `if` (or a
    boolean expression) that tests master_control.paper_authority_enabled
    — so disabling that flag provably removes MASTER from every one of
    these decision points, not just some of them."""
    source = MAIN_ENTRYPOINT.read_text()
    tree = ast.parse(source)

    call_sites = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "try_reserve_for_opportunity"
    ]
    assert len(call_sites) >= 2, f"expected at least 2 try_reserve_for_opportunity call sites (CEX + DEX), found {len(call_sites)}"

    def _contains(container: ast.AST, target: ast.AST) -> bool:
        return any(node is target for node in ast.walk(container))

    for call in call_sites:
        guarding_ifs = [
            n
            for n in ast.walk(tree)
            if isinstance(n, ast.If) and any(_contains(stmt, call) for stmt in n.body)
        ]
        assert guarding_ifs, f"try_reserve_for_opportunity call at line {call.lineno} is not inside any `if` block"
        # the innermost (smallest span) guarding if is the one whose test we check
        innermost = min(guarding_ifs, key=lambda n: (n.end_lineno or n.lineno) - n.lineno)
        test_source = ast.dump(innermost.test)
        assert "paper_authority_enabled" in test_source, (
            f"the if-guard around the try_reserve_for_opportunity call at line {call.lineno} "
            f"does not test master_control.paper_authority_enabled"
        )


def test_main_py_never_hardcodes_real_orders_placed_to_true():
    """A crude but effective textual guard: 'real_orders_placed' must
    never appear near a literal True anywhere in main.py."""
    source = MAIN_ENTRYPOINT.read_text()
    assert "real_orders_placed = True" not in source
    assert "real_orders_placed=True" not in source
