"""RECONCILIATION SELF-HEALING (user directive, 2026-08-25, AUTONOMOUS
SELF-HEALING OPERATIONS LAYER, item 3). The procedure: re-read balances,
re-read real trades/orders, reconstruct events, check whether the gap is
explained by a known event type not yet integrated into the original
reconcile call, re-reconcile. AUTO_RECOVERED if explained, SAFE STOP if
not -- "ne jamais inventer une correction de ledger."

This module is the pure decision core only: given an already-computed
ReconciliationResult and a small set of CANDIDATE explaining events (each
one a real, independently-fetched quantity -- e.g. a rebalance sell this
cycle actually executed, confirmed against real trade history, never an
invented number), it searches for the smallest subset that explains the
mismatch within the original tolerance. Fetching the candidate events
themselves (re-reading balances/trades) is necessarily I/O and belongs in
the orchestrator, exactly like every other pure/IO split in this
project's app.execution modules.

FIX 3 to reconcile_base_asset_balance (2026-08-25) already closes the
ONE specific gap this module exists to guard against going forward (a
rebalance sell in the same cycle) -- callers that pass complete data
should never need this module's search to find anything. It remains the
safety net for a genuinely new gap shape, and the mechanism this
project's real RVN incident would have been caught by even before that
targeted fix existed."""

from dataclasses import dataclass
from itertools import combinations

from app.execution.reconciliation import ReconciliationResult


@dataclass(slots=True, frozen=True)
class CandidateExplanationEvent:
    label: str
    signed_qty: float  # positive = added to the balance, negative = removed -- always from real, independently-verified trade data


@dataclass(slots=True, frozen=True)
class SelfHealingResult:
    recovered: bool
    final_result: ReconciliationResult
    explaining_events: tuple[CandidateExplanationEvent, ...]
    diagnostic: str


def attempt_reconciliation_recovery(
    *, original_result: ReconciliationResult, candidate_events: tuple[CandidateExplanationEvent, ...],
) -> SelfHealingResult:
    """Pure. Tries subsets of `candidate_events` smallest-first (the
    simplest explanation that fits wins) and returns the first one whose
    combined signed quantity brings the difference within the ORIGINAL
    tolerance -- never a loosened one. If `original_result` already
    matches, there is nothing to heal. If no combination explains it,
    returns recovered=False: this is a real SAFE STOP, not a search
    failure to retry with different numbers."""
    if original_result.match:
        return SelfHealingResult(True, original_result, (), "no mismatch -- nothing to heal")

    for r in range(1, len(candidate_events) + 1):
        for combo in combinations(candidate_events, r):
            adjustment = sum(e.signed_qty for e in combo)
            new_expected = original_result.expected_delta + adjustment
            new_difference = original_result.actual_delta - new_expected
            if abs(new_difference) <= original_result.tolerance:
                labels = ", ".join(e.label for e in combo)
                explanation = (
                    f"AUTO_RECOVERED: original mismatch (difference={original_result.difference:.6f}) fully "
                    f"explained by real event(s) not in the original reconciliation call: {labels} "
                    f"(combined adjustment {adjustment:+.6f}); revised expected_delta={new_expected:.6f}, "
                    f"revised difference={new_difference:.6f}, tolerance={original_result.tolerance:.6f}"
                )
                healed = ReconciliationResult(
                    expected_delta=new_expected, actual_delta=original_result.actual_delta,
                    difference=new_difference, tolerance=original_result.tolerance, match=True,
                    explanation=explanation,
                )
                return SelfHealingResult(True, healed, combo, explanation)

    return SelfHealingResult(
        False, original_result, (),
        f"SAFE STOP: mismatch (difference={original_result.difference:.6f}, tolerance={original_result.tolerance:.6f}) "
        f"could not be explained by any combination of the {len(candidate_events)} real candidate event(s) checked "
        "-- never inventing a correction; this becomes a CRITICAL_SAFETY incident for human review",
    )
