"""RECOVERY ACTION DECISION (user directive, 2026-08-25, AUTONOMOUS
SELF-HEALING OPERATIONS LAYER, items 2 and 5). Turns a classified
incident into a concrete RecoveryPlan. Pure, no I/O, no order placement.

Two pieces feed the decision:

1. incident_taxonomy.classify_incident() -- the mechanical, structural
   mapping (item 1's taxonomy -> item 5's levels) that already handles
   almost everything: CAPITAL_REBALANCE/INVENTORY/RECONCILIATION/
   AMBIGUOUS_ORDER_STATE/ORDER_REJECTED_SAFE all land at LEVEL 2-3, and
   the category itself tells the orchestrator WHICH already-built,
   already-tested mechanism to invoke (the Capital Rebalancer for
   CAPITAL_REBALANCE, classify_candidate/compute_inventory_topup_plan
   for INVENTORY, attempt_reconciliation_recovery for RECONCILIATION,
   one authoritative status re-read for AMBIGUOUS_ORDER_STATE). This
   module does not re-decide those mechanisms -- they already exist and
   are already tested elsewhere; it only decides the ACTION/SCOPE.

2. classify_script_exception() -- item 2's explicit requirement to
   "distinguer strictement erreur avant order submission / etat ambigu
   apres submission" for a RAW, otherwise-unclassified exception (the
   "SCRIPT ERROR" catch-all). A plain network exception (timeout,
   connection error, HTTP 429/5xx) caught strictly BEFORE any order
   submission is safely retryable; the exact same exception caught AFTER
   a submission call is never blindly retried, because a real order may
   already exist. An unrecognized exception type is never assumed
   benign either way -- CRITICAL_SAFETY is the only zero-guessing
   default that costs nothing but a pause.

Neither function ever touches the KB's safe_recovery text as a
substitute for these structural rules -- the KB (item 4) only ANNOTATES
a decision that was already reached structurally, for the activity feed
and the replay report; it never overrides a CRITICAL_SAFETY level 5, on
the principle that a known-fixed bug recurring despite its fix is more
alarming, not less."""

from dataclasses import dataclass
from enum import StrEnum

from app.operations.incident_knowledge_base import KnownIncident
from app.operations.incident_taxonomy import (
    IncidentCategory,
    IncidentClassification,
    RecoveryLevel,
    RecoveryScope,
    classify_incident,
)

# Exception class names (not the exception objects themselves, to keep
# this pure and dependency-free) known to be plain network/transport
# failures with no possibility of having reached the exchange's matching
# engine -- safe to retry with backoff ONLY when raised strictly before
# any order-submission call.
_KNOWN_TRANSIENT_EXCEPTION_NAMES = frozenset({
    "ClientConnectionError", "ClientConnectorError", "ClientOSError", "ClientPayloadError",
    "ServerDisconnectedError", "ServerTimeoutError", "TimeoutError", "asyncio.TimeoutError",
    "ConnectionResetError", "ConnectionRefusedError",
})


class RecoveryActionType(StrEnum):
    CONTINUE = "CONTINUE"  # LEVEL 0
    RETRY = "RETRY"  # LEVEL 1
    RECALCULATE_AND_RETRY = "RECALCULATE_AND_RETRY"  # LEVEL 2
    ISOLATE_OPPORTUNITY = "ISOLATE_OPPORTUNITY"  # LEVEL 3
    ISOLATE_EXCHANGE = "ISOLATE_EXCHANGE"  # LEVEL 4
    GLOBAL_SAFE_STOP = "GLOBAL_SAFE_STOP"  # LEVEL 5, a known safety boundary
    CODE_FIX_REQUIRED = "CODE_FIX_REQUIRED"  # LEVEL 5, a genuinely new/unclassified problem


_LEVEL_TO_ACTION: dict[RecoveryLevel, RecoveryActionType] = {
    RecoveryLevel.LEVEL_0_IGNORE: RecoveryActionType.CONTINUE,
    RecoveryLevel.LEVEL_1_RETRY_SAFE: RecoveryActionType.RETRY,
    RecoveryLevel.LEVEL_2_RECALCULATE: RecoveryActionType.RECALCULATE_AND_RETRY,
    RecoveryLevel.LEVEL_3_ISOLATE_OPPORTUNITY: RecoveryActionType.ISOLATE_OPPORTUNITY,
    RecoveryLevel.LEVEL_4_ISOLATE_EXCHANGE: RecoveryActionType.ISOLATE_EXCHANGE,
}


@dataclass(slots=True, frozen=True)
class RecoveryPlan:
    action: RecoveryActionType
    level: RecoveryLevel
    scope: RecoveryScope
    category: IncidentCategory
    reason: str
    known_incident: KnownIncident | None


def classify_script_exception(exception_class_name: str, *, order_submission_attempted: bool) -> IncidentClassification:
    """Pure. The item-2 API/network rule for a raw, otherwise
    unclassified exception. `order_submission_attempted` must be True
    for ANY exception raised at or after the order-submission call
    itself (never only for confirmed successes) -- when in doubt about
    which side of that line an exception fell on, the caller must pass
    True."""
    if order_submission_attempted:
        return IncidentClassification(
            IncidentCategory.AMBIGUOUS_ORDER_STATE, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
            f"exception ({exception_class_name}) raised at or after order submission -- a real order may already "
            "exist; never blind-retry submission, so this is never auto-resolved below LEVEL 5",
        )
    if exception_class_name in _KNOWN_TRANSIENT_EXCEPTION_NAMES:
        return IncidentClassification(
            IncidentCategory.TRANSIENT, RecoveryLevel.LEVEL_1_RETRY_SAFE, RecoveryScope.NONE,
            f"{exception_class_name} raised strictly before any order submission -- a known plain network "
            "failure, safe to retry with backoff",
        )
    return IncidentClassification(
        IncidentCategory.CODE_LOGIC_UNKNOWN, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        f"{exception_class_name} raised before order submission but is not a recognized transient network type -- "
        "never assumed benign; routed to CODE_FIX_REQUIRED",
    )


def decide_recovery_plan(
    *, incident_type: str, known_incident: KnownIncident | None = None,
    classification: IncidentClassification | None = None,
) -> RecoveryPlan:
    """Pure. `classification` may be pre-computed (e.g. via
    classify_script_exception for a raw exception); otherwise it is
    derived from `incident_type` via incident_taxonomy.classify_incident.
    A KB match never downgrades a LEVEL 5 classification -- it only
    enriches the plan's `reason` with the already-validated safe_recovery
    text (for the activity feed / replay report) and, for LEVEL 0-4
    categories, confirms this exact shape has succeeded before."""
    c = classification if classification is not None else classify_incident(incident_type)

    if c.level == RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP:
        action = (
            RecoveryActionType.CODE_FIX_REQUIRED
            if c.category == IncidentCategory.CODE_LOGIC_UNKNOWN and known_incident is None
            else RecoveryActionType.GLOBAL_SAFE_STOP
        )
        reason = c.reason
        if known_incident is not None:
            reason += f" | KB match ({known_incident.incident_signature}, seen {known_incident.occurrence_count}x before) -- still LEVEL 5 by design, a recurrence of a known-fixed problem is more alarming, not less"
        return RecoveryPlan(action=action, level=c.level, scope=c.scope, category=c.category, reason=reason, known_incident=known_incident)

    action = _LEVEL_TO_ACTION[c.level]
    reason = c.reason
    if known_incident is not None:
        reason += f" | KB match ({known_incident.incident_signature}): {known_incident.safe_recovery}"
    return RecoveryPlan(action=action, level=c.level, scope=c.scope, category=c.category, reason=reason, known_incident=known_incident)
