"""DANGEROUS MODULE GUARD (user directive, 2026-08-25, V5 three-exchange
shadow -- "L'isolation empechant V5 Shadow de placer des ordres doit
rester structurellement garantie et testee"). A VPS-only orchestrator
script (never committed to git, matching the v3/v4 precedent) cannot be
reached directly by tests/test_phase3a_isolation.py's own AST scan of
app/. This module is the tested primitive such a script calls at its own
startup, against its own `sys.modules`, to get the SAME guarantee
enforced at every real run rather than only in CI: refuse to proceed if
any order-capable client module has been loaded into the process,
whether imported directly or transitively through anything else it
imported."""

from collections.abc import Iterable

DEFAULT_DANGEROUS_MODULES: tuple[str, ...] = ("binance_live_trade_client", "bybit_live_trade_client", "okx_live_trade_client")


def find_loaded_dangerous_modules(
    loaded_module_names: Iterable[str], dangerous_substrings: tuple[str, ...] = DEFAULT_DANGEROUS_MODULES,
) -> list[str]:
    """Pure. Returns every loaded module name containing any of the
    dangerous substrings -- empty list means the guarantee holds. Never
    reads sys.modules itself (that stays at the caller's own edge, e.g.
    `find_loaded_dangerous_modules(sys.modules.keys())`), so this
    function is fully deterministic and testable without a real import
    happening anywhere."""
    return [name for name in loaded_module_names if any(d in name for d in dangerous_substrings)]
