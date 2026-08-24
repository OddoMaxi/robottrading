"""MISSED PROFITABLE OPPORTUNITIES (V2.1, user directive, 2026-08-24,
item 5) — classifies every STAGE B result the scanner evaluates into
either "not a miss" (net-positive, executable, clears the safety margin
and latency bar — the shape of thing that WOULD be actioned if real
execution were authorized) or one of the stated causes, carrying the
theoretical net profit that would have been captured — always tracked
as THEORETICAL_NOT_REALIZED, never as a real fill. No order is placed
anywhere in this module.

Pure classification (classify_miss) plus two small in-memory
accumulators, process-local to altcoin_scanner.py (same lifetime/shape
as app.scanner.continuity_tracker.ContinuityTracker):

MissedOpportunityTracker — quote-level causes (INSUFFICIENT_DEPTH,
MIN_NOTIONAL, FEES, SAFETY_MARGIN, LATENCY, EDGE_DISAPPEARED, OTHER),
derivable from a single DualLegQuote plus the tick-to-tick transition
EdgeDisappearanceTracker below detects.

EdgeDisappearanceTracker — kept separate from ContinuityTracker
(rather than modifying that already-tested class) — needs to compare
THIS tick to the LAST tick for the same (symbol, buy, sell) key, which
ContinuityTracker's own observe() doesn't expose.

CAPITAL_BUSY, POSITION_ALREADY_OPEN and INVENTORY_MISSING are
deliberately NOT classified here — a DualLegQuote has no notion of real
account balances or in-flight positions. Those three causes are
computed at the reporting layer instead
(app.reporting.missed_opportunity_report), using live balance/live_guard
state that only the engine process (a separate systemd service) has.
"""

from dataclasses import dataclass, field

CAUSE_INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
CAUSE_MIN_NOTIONAL = "MIN_NOTIONAL"
CAUSE_FEES = "FEES"
CAUSE_SAFETY_MARGIN = "SAFETY_MARGIN"
CAUSE_LATENCY = "LATENCY"
CAUSE_EDGE_DISAPPEARED = "EDGE_DISAPPEARED"
CAUSE_OTHER = "OTHER"
CAUSE_INVENTORY_MISSING = "INVENTORY_MISSING"  # classified at the reporting layer, listed here so every consumer sees the full taxonomy
CAUSE_CAPITAL_BUSY = "CAPITAL_BUSY"  # classified at the reporting layer
CAUSE_POSITION_ALREADY_OPEN = "POSITION_ALREADY_OPEN"  # classified at the reporting layer

QUOTE_LEVEL_CAUSES = (CAUSE_INSUFFICIENT_DEPTH, CAUSE_MIN_NOTIONAL, CAUSE_FEES, CAUSE_SAFETY_MARGIN, CAUSE_LATENCY, CAUSE_EDGE_DISAPPEARED, CAUSE_OTHER)
ALL_CAUSES = QUOTE_LEVEL_CAUSES + (CAUSE_INVENTORY_MISSING, CAUSE_CAPITAL_BUSY, CAUSE_POSITION_ALREADY_OPEN)

# Established elsewhere in this codebase (app.execution.dual_leg_quote /
# app.scanner.market_snapshot) as the sentinel for "depth couldn't fill
# the requested quantity at all" — reused here, not reinvented.
DEPTH_SLIPPAGE_SENTINEL_PCT = 100.0

# Stated, round assumption (item 4: real thresholds only, never lowered
# to inflate a counter) — a dual-leg confirmation this slow means real
# execution would very plausibly have missed the window. Not fitted to
# any observed data.
DEFAULT_LATENCY_MISS_THRESHOLD_MS = 2000.0


def classify_miss(quote, safety_margin_usd: float, latency_threshold_ms: float = DEFAULT_LATENCY_MISS_THRESHOLD_MS) -> tuple[str | None, float]:
    """Returns (cause, theoretical_net_profit_usd). cause is None when
    this quote is NOT a miss — a genuine, currently-actionable
    opportunity (net-positive, executable, clears the safety margin and
    latency bar), not a missed one."""
    if quote.buy_slippage_pct >= DEPTH_SLIPPAGE_SENTINEL_PCT or quote.sell_slippage_pct >= DEPTH_SLIPPAGE_SENTINEL_PCT:
        return CAUSE_INSUFFICIENT_DEPTH, max(0.0, quote.net_profit_usd)

    if not quote.executable:
        reason = quote.reason or ""
        if "not tradable" in reason:
            return CAUSE_OTHER, 0.0
        if "min_qty" in reason or "min_notional" in reason:
            return CAUSE_MIN_NOTIONAL, 0.0
        if "net_profit_usd" in reason:
            # gross spread existed but real fees/slippage erased it —
            # theoretical profit is <=0 by this same reason string, so
            # there's nothing positive to record as "not realized".
            return CAUSE_FEES, 0.0
        return CAUSE_OTHER, 0.0

    # executable=True from here — compute_dual_leg_quote's own gate
    # already guarantees net_profit_usd > 0 at this point.
    if quote.net_profit_usd - safety_margin_usd <= 0:
        return CAUSE_SAFETY_MARGIN, quote.net_profit_usd
    if quote.dual_leg_latency_ms > latency_threshold_ms:
        return CAUSE_LATENCY, quote.net_profit_usd
    return None, quote.net_profit_usd


@dataclass(slots=True)
class MissedOpportunityAccumulator:
    count: int = 0
    theoretical_profit_usd_total: float = 0.0


class MissedOpportunityTracker:
    def __init__(self) -> None:
        self._by_cause: dict[str, MissedOpportunityAccumulator] = {cause: MissedOpportunityAccumulator() for cause in QUOTE_LEVEL_CAUSES}

    def record(self, cause: str, theoretical_profit_usd: float) -> None:
        acc = self._by_cause.setdefault(cause, MissedOpportunityAccumulator())
        acc.count += 1
        acc.theoretical_profit_usd_total += max(0.0, theoretical_profit_usd)

    def snapshot(self) -> dict[str, MissedOpportunityAccumulator]:
        return {cause: MissedOpportunityAccumulator(acc.count, acc.theoretical_profit_usd_total) for cause, acc in self._by_cause.items()}


class EdgeDisappearanceTracker:
    """Positive -> non-positive transitions per (symbol, buy, sell) —
    kept separate from ContinuityTracker (see module docstring)."""

    def __init__(self) -> None:
        self._last_positive_net_profit: dict[tuple[str, str, str], float] = {}

    def observe(self, symbol: str, buy_exchange: str, sell_exchange: str, is_positive: bool, net_profit_usd: float) -> float | None:
        """Returns the theoretical profit that just disappeared, or
        None if this tick isn't a disappearance."""
        key = (symbol, buy_exchange, sell_exchange)
        if is_positive:
            self._last_positive_net_profit[key] = net_profit_usd
            return None
        return self._last_positive_net_profit.pop(key, None)
