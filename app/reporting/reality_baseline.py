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
restarted 2026-08-22 10:24:34 UTC) — not an estimate.
"""

from datetime import datetime

REALITY_BASELINE_AT = datetime(2026, 8, 22, 10, 24, 34)  # UTC, naive — matches this codebase's datetime.now(UTC).replace(tzinfo=None) convention


def hours_since_baseline(now: datetime) -> float:
    delta = (now - REALITY_BASELINE_AT).total_seconds() / 3600.0
    return max(delta, 0.0)


def window_contains_pre_baseline_data(now: datetime, hours: float) -> bool:
    """True if a lookback window of `hours` ending at `now` reaches back
    past REALITY_BASELINE_AT — the dashboard must visibly flag this rather
    than presenting the window as clean audited data."""
    window_start = now.timestamp() - hours * 3600.0
    return window_start < REALITY_BASELINE_AT.timestamp()
