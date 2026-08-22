"""Reality Baseline (V5/V5.5 Master Orchestration, user directive,
2026-08-22, spec Part L).

The 2026-08-22 Reality Audit found and fixed two bugs that materially
inflated DEX paper-trading results before this baseline: atomic/sequential
double-counting (main.py's dedup fix) and no real capital concurrency
(app.onchain.dex_paper_trader.DexCapitalPool's time-windowed reservations).
Data detected/executed BEFORE the fix's deploy is contaminated by those
bugs and must never silently blend into a window the dashboard presents
as "audited" — spec Part L's own rule: "Never make old double-counted
simulated profit appear as audited profit."

REALITY_BASELINE_AT is the fix's actual deploy timestamp, read from the
VPS clock immediately after the post-fix restart (main.py commit c219065,
restarted 2026-08-22 10:24:34 UTC) — not an estimate. This is "REALITY
AUDIT #1" — preserved permanently, never overwritten by a later baseline.

PRE_PHASE_2_VALIDATION_BASELINE_AT (PRE-PHASE-2 CORRECTIVE MAINTENANCE,
user directive, 2026-08-22, spec Part 7) is a SECOND, later baseline for
the post-corrective-maintenance restart — fixing two more bugs found
during REALITY AUDIT #1's own Mission 3 verification: (1) a $0.01 CEX
ledger rounding-drift bug (app.simulation.money's own docstring has the
full root-cause trace), and (2) update_opportunity_tracking silently
erasing the duplicate_economic_event marking on every DEX continuation
update (main.py's DEX detection loop). Data between REALITY_BASELINE_AT
and PRE_PHASE_2_VALIDATION_BASELINE_AT is NOT retroactively repaired —
per spec Part 4's own rule, older rows that can't be reliably repaired
are marked LEGACY / NOT RELIABLE rather than silently patched or
invented. Specifically: duplicate_economic_event COUNTS in that window
are a confirmed undercount (~50%, see the Reality Audit final report's
Mission 3) — the underlying capital-safety guarantee (no opportunity was
ever double-executed) was independently verified by direct pool/timestamp
matching and remains valid for that whole window regardless; only the
REPORTED counter was wrong. Left as None until the controlled restart in
spec Part 6 actually happens — never set to a placeholder or estimated
value ahead of time.
"""

from datetime import datetime

REALITY_BASELINE_AT = datetime(2026, 8, 22, 10, 24, 34)  # UTC, naive — matches this codebase's datetime.now(UTC).replace(tzinfo=None) convention

# Set to the exact post-corrective-maintenance restart timestamp once that
# restart actually happens (spec Part 6) — read from the VPS clock, same
# discipline as REALITY_BASELINE_AT itself. None means "not yet restarted".
PRE_PHASE_2_VALIDATION_BASELINE_AT: datetime | None = None

# The window between the two baselines has a confirmed, disclosed
# reporting limitation (NOT a capital-safety issue) — see module docstring.
LEGACY_DUPLICATE_COUNT_WINDOW_NOTE = (
    "duplicate_economic_event counts between REALITY_BASELINE_AT (2026-08-22 10:24:34 UTC) and "
    "PRE_PHASE_2_VALIDATION_BASELINE_AT are a confirmed undercount (~50%) due to a since-fixed bug "
    "in update_opportunity_tracking — marked LEGACY / NOT RELIABLE for that window specifically. "
    "No opportunity was ever double-executed in this window (independently verified by direct "
    "pool/timestamp matching, immune to the counter bug) — this is a reporting-accuracy issue only."
)


def hours_since_baseline(now: datetime, baseline: datetime = REALITY_BASELINE_AT) -> float:
    delta = (now - baseline).total_seconds() / 3600.0
    return max(delta, 0.0)


def window_contains_pre_baseline_data(now: datetime, hours: float, baseline: datetime = REALITY_BASELINE_AT) -> bool:
    """True if a lookback window of `hours` ending at `now` reaches back
    past `baseline` — the dashboard must visibly flag this rather than
    presenting the window as clean audited data."""
    window_start = now.timestamp() - hours * 3600.0
    return window_start < baseline.timestamp()
