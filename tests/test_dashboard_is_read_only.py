"""Continuous Execution spec, section 57: "state before dashboard refresh =
state after dashboard refresh" if no new market event arrived. The dashboard
runs in a completely separate process from the engine (main.py) and only
ever SELECTs — this is a static guard against a future change accidentally
wiring a write path (or a detection/paper-trading call) into it, which
would silently break that guarantee.
"""

from pathlib import Path

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"

# Anything that would mutate opportunities/trades/portfolios, or perform
# detection/paper-trading — none of this may ever appear in dashboard code.
FORBIDDEN_CALLS = [
    "save_opportunity(",
    "save_simulated_trade(",
    "save_price_snapshots(",
    "update_opportunity_tracking(",
    "close_opportunity_tracking(",
    ".scan_once(",
    ".detect(",
    "paper_trader.simulate(",
    "paper_trader.determine_outcome(",
    "position_tracker.open_position(",
    "OpportunityDetector(",
    "PaperTrader(",
]


def test_dashboard_source_never_calls_a_mutating_or_engine_function():
    dashboard_files = list(DASHBOARD_DIR.glob("*.py"))
    assert dashboard_files, "expected to find dashboard/*.py files"

    violations = []
    for path in dashboard_files:
        source = path.read_text()
        for forbidden in FORBIDDEN_CALLS:
            if forbidden in source:
                violations.append(f"{path.name}: found {forbidden!r}")

    assert violations == [], "Dashboard must be read-only — found engine/mutation calls:\n" + "\n".join(violations)


def test_dashboard_data_layer_only_ever_selects():
    """Every SQLAlchemy statement dashboard/data.py builds must be a SELECT
    — no insert/update/delete, regardless of what helper it goes through."""
    source = (DASHBOARD_DIR / "data.py").read_text()
    assert "insert(" not in source
    assert "update(" not in source
    assert "delete(" not in source
