"""Shadow Economic-Event Deduplication (Phase 2, SHADOW MODE ONLY —
corrective maintenance #2, user directive, 2026-08-22).

FIX: MASTER previously evaluated every persisted opportunity row
independently, with no notion that an `atomic` opportunity and its
`dex_triangular`/`dex_multihop`/`dex_cross` sequential sibling describe
the SAME real-world price gap (identical legs + identical detected_at —
the same identity app.reporting.dex_execution_funnel and the original
Reality Audit both used to find the real double-counting bug this
mirrors). Without this, MASTER "allocated" capital and credited
projected P&L to BOTH twins of essentially every pair — the root cause
of the 0% OLD-vs-MASTER agreement on `atomic` found in the first Shadow
Mode validation (OLD correctly rejects the loser as
duplicate_economic_event; MASTER, before this fix, didn't know they were
the same event at all).

MASTER computes this INDEPENDENTLY from the persisted legs+detected_at —
it does NOT simply trust/copy OLD's own duplicate_economic_event
rejection_reason (that would make MASTER's dedup entirely derived from
OLD, not an independent capability of its own). Given a duplicate pair,
MASTER picks the higher-ranked representative using the SAME
app.shadow.ranker.compute_master_rank_score it would use to allocate
capital anyway, so the "which twin wins" decision is consistent with the
rest of Master's own logic, not an arbitrary tie-break.
"""

import json
import uuid

from app.shadow.models import ShadowOpportunitySummary
from app.shadow.ranker import compute_master_rank_score


def economic_event_key(opp: ShadowOpportunitySummary) -> str:
    """Same identity CEX/DEX opportunities share when they describe one
    real-world event: exact detected_at timestamp + exact legs content
    (pools, prices, sides, direction) — the atomic-bundling variant of a
    cross-DEX/multi-hop opportunity is constructed from the SAME legs at
    the SAME instant as its sequential sibling, by main.py's own
    detection code."""
    legs_repr = json.dumps(opp.legs, sort_keys=True) if opp.legs else ""
    return f"{opp.detected_at.isoformat()}|{legs_repr}"


def partition_duplicate_economic_events(
    opportunities: list[ShadowOpportunitySummary],
) -> tuple[list[ShadowOpportunitySummary], dict[uuid.UUID, ShadowOpportunitySummary]]:
    """Returns (representatives, duplicate_losers) — representatives is
    exactly one opportunity per unique economic event (the
    highest-master-rank-score candidate within any group sharing
    detected_at+legs; an opportunity with no legs can never be grouped
    with anything, so it's always its own singleton event).
    duplicate_losers maps every OTHER opportunity's id to the winning
    representative it duplicates — these must never be independently
    evaluated for capital."""
    groups: dict[str, list[ShadowOpportunitySummary]] = {}
    for opp in opportunities:
        key = economic_event_key(opp) if opp.legs else f"__no_legs__:{opp.opportunity_id}"
        groups.setdefault(key, []).append(opp)

    representatives: list[ShadowOpportunitySummary] = []
    duplicate_losers: dict[uuid.UUID, ShadowOpportunitySummary] = {}
    for group in groups.values():
        if len(group) == 1:
            representatives.append(group[0])
            continue
        ranked = sorted(group, key=lambda o: compute_master_rank_score(o) or float("-inf"), reverse=True)
        winner = ranked[0]
        representatives.append(winner)
        for loser in ranked[1:]:
            duplicate_losers[loser.opportunity_id] = winner
    return representatives, duplicate_losers
