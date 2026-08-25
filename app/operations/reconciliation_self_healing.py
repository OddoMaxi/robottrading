"""RECONCILIATION SELF-HEALING (user directive, 2026-08-25, AUTONOMOUS
SELF-HEALING OPERATIONS LAYER item 3; rebuilt 2026-08-25 for FIX 4 --
MULTI-ASSET RECONCILIATION, item 5). The procedure: re-read balances,
re-read real trades/orders, reconstruct events, group by (exchange,
asset), check whether the gap is explained by a known event not yet
included, re-reconcile. AUTO_RECOVERED if explained, SAFE STOP if not --
"ne jamais inventer une correction de ledger."

Given app.execution.reconciliation.reconcile_asset_balance already
filters every event to the exact (exchange, asset) pair being checked
before summing anything, the original ZIL/RVN incident this module was
extended to guard against can no longer occur through this module's own
mechanism -- a candidate event for the wrong asset is filtered out
structurally, not by search logic. What remains for self-healing to do:
search ADDITIONAL real, independently-verified candidate events (for the
SAME asset) not originally included in the reconciliation call, exactly
as before FIX 4 -- plus explicitly detect and report the case where a
caller offers a candidate for a DIFFERENT asset (CROSS_ASSET_
RECONCILIATION_ATTEMPT, item 5): such a candidate is never used as an
explanation even if its magnitude would numerically close the gap, and
is reported by name rather than silently dropped."""

from dataclasses import dataclass
from itertools import combinations

from app.execution.reconciliation import AssetReconciliationResult, LedgerEvent, reconcile_asset_balance


@dataclass(slots=True, frozen=True)
class SelfHealingResult:
    recovered: bool
    final_result: AssetReconciliationResult
    explaining_events: tuple[LedgerEvent, ...]
    cross_asset_attempts_rejected: tuple[LedgerEvent, ...]
    diagnostic: str


def attempt_reconciliation_recovery(
    *, original_result: AssetReconciliationResult, candidate_events: tuple[LedgerEvent, ...],
) -> SelfHealingResult:
    """Pure. `candidate_events` are real, independently-verified events
    not part of the original reconciliation call -- some may legitimately
    belong to `original_result.asset`, others may belong to a different
    asset entirely (a rebalance of some other holding that happened on
    the same exchange). Only same-(exchange, asset) candidates are ever
    searched as an explanation; any others are reported as rejected
    CROSS_ASSET_RECONCILIATION_ATTEMPTs, never silently used even if
    their magnitude would numerically close the gap. Tries subsets
    smallest-first (the simplest explanation that fits wins) against the
    ORIGINAL tolerance -- never a loosened one."""
    same_asset = tuple(e for e in candidate_events if e.exchange == original_result.exchange and e.base_asset == original_result.asset)
    cross_asset_rejected = tuple(e for e in candidate_events if not (e.exchange == original_result.exchange and e.base_asset == original_result.asset))

    if original_result.match:
        return SelfHealingResult(True, original_result, (), (), "no mismatch -- nothing to heal")

    for r in range(1, len(same_asset) + 1):
        for combo in combinations(same_asset, r):
            adjustment = sum(e.net_base_delta for e in combo)
            new_expected = original_result.expected_delta + adjustment
            new_difference = original_result.actual_delta - new_expected
            if abs(new_difference) <= original_result.tolerance:
                labels = ", ".join(f"{e.event_type.value}({e.base_asset}{', order ' + e.order_id if e.order_id else ''})" for e in combo)
                explanation = (
                    f"AUTO_RECOVERED: original mismatch (difference={original_result.difference:.6f}) for "
                    f"{original_result.exchange}/{original_result.asset} fully explained by real, same-asset "
                    f"event(s) not in the original reconciliation call: {labels} (combined adjustment {adjustment:+.6f}); "
                    f"revised expected_delta={new_expected:.6f}, revised difference={new_difference:.6f}, "
                    f"tolerance={original_result.tolerance:.6f}"
                )
                if cross_asset_rejected:
                    rejected_labels = ", ".join(f"{e.event_type.value}({e.base_asset})" for e in cross_asset_rejected)
                    explanation += f" | CROSS_ASSET_RECONCILIATION_ATTEMPT also detected and refused (not used): {rejected_labels}"
                healed = AssetReconciliationResult(
                    exchange=original_result.exchange, asset=original_result.asset, expected_delta=new_expected,
                    actual_delta=original_result.actual_delta, difference=new_difference, tolerance=original_result.tolerance,
                    match=True, contributing_events=original_result.contributing_events + combo, explanation=explanation,
                )
                return SelfHealingResult(True, healed, combo, cross_asset_rejected, explanation)

    detail = (
        f"SAFE STOP: mismatch (difference={original_result.difference:.6f}, tolerance={original_result.tolerance:.6f}) "
        f"for {original_result.exchange}/{original_result.asset} could not be explained by any combination of the "
        f"{len(same_asset)} real same-asset candidate event(s) checked -- never inventing a correction"
    )
    if cross_asset_rejected:
        rejected_labels = ", ".join(f"{e.event_type.value}({e.base_asset}, net_base_delta={e.net_base_delta})" for e in cross_asset_rejected)
        detail += (
            f" | CROSS_ASSET_RECONCILIATION_ATTEMPT detected and refused: {rejected_labels} -- one or more of these "
            f"would have numerically fit the gap, but belong to a different asset than {original_result.asset}; "
            "never used as an explanation, regardless of fit"
        )
    detail += " -- this becomes a CRITICAL_SAFETY incident for human review"
    return SelfHealingResult(False, original_result, (), cross_asset_rejected, detail)
