"""INCIDENT CLASSIFICATION (user directive, 2026-08-25, AUTONOMOUS
SELF-HEALING OPERATIONS LAYER, item 1). Maps the incident "type" strings
this codebase's live executors/orchestrators already produce (see
continuous_live_session_v2.py's `incident = {"type": ..., ...}` dicts,
app.execution.live_arbitrage_executor's ArbitrageOutcome, and
app.execution.capital_rebalancer's error strings) onto a fixed taxonomy,
an auto-recovery level, and a blast-radius scope -- the SAME vocabulary
already flowing through this project's real incidents, never a parallel
or invented one. Pure, no I/O, no order placement.

Matching is by stable substring/prefix, not exact string equality,
because several real incident types carry a dynamic suffix identifying
which leg was involved (e.g. "UNKNOWN ORDER STATE (buy leg)" vs
"...(rebalance leg)") -- the category and level are the same regardless
of which leg, so matching on the stable prefix is correct and keeps this
table from rotting every time a new leg name is added at a call site.

Checked in priority order (most safety-critical first) so that if a type
string could plausibly match more than one entry, the safer
classification wins."""

from dataclasses import dataclass
from enum import IntEnum, StrEnum


class IncidentCategory(StrEnum):
    TRANSIENT = "TRANSIENT"
    RECOVERABLE_OPERATIONAL = "RECOVERABLE_OPERATIONAL"
    CAPITAL_REBALANCE = "CAPITAL_REBALANCE"
    INVENTORY = "INVENTORY"
    EXCHANGE_CONSTRAINT = "EXCHANGE_CONSTRAINT"
    DATA_STALE = "DATA_STALE"
    ORDER_REJECTED_SAFE = "ORDER_REJECTED_SAFE"
    AMBIGUOUS_ORDER_STATE = "AMBIGUOUS_ORDER_STATE"
    RECONCILIATION = "RECONCILIATION"
    CODE_LOGIC_UNKNOWN = "CODE_LOGIC_UNKNOWN"
    CRITICAL_SAFETY = "CRITICAL_SAFETY"


class RecoveryLevel(IntEnum):
    """Ordered (unlike this codebase's usual StrEnum status types)
    because severity comparisons genuinely matter here: a caller needs
    to ask "is this at least as bad as ISOLATE_EXCHANGE" without a
    lookup table."""

    LEVEL_0_IGNORE = 0
    LEVEL_1_RETRY_SAFE = 1
    LEVEL_2_RECALCULATE = 2
    LEVEL_3_ISOLATE_OPPORTUNITY = 3
    LEVEL_4_ISOLATE_EXCHANGE = 4
    LEVEL_5_GLOBAL_SAFE_STOP = 5


class RecoveryScope(StrEnum):
    NONE = "NONE"  # nothing to isolate -- the loop just continues
    SYMBOL_DIRECTION = "SYMBOL_DIRECTION"
    EXCHANGE = "EXCHANGE"
    GLOBAL = "GLOBAL"


@dataclass(slots=True, frozen=True)
class IncidentClassification:
    category: IncidentCategory
    level: RecoveryLevel
    scope: RecoveryScope
    reason: str

    @property
    def auto_recoverable(self) -> bool:
        """LEVEL 0-4 are handled by this layer without a human in the
        loop (even LEVEL 4 -- isolating an exchange is still an
        autonomous operational decision, not a stop). Only LEVEL 5 ever
        requires SAFE STOP -> REPORT -> WAIT, per the user's own item 9
        ("auto-modification du code... attendre validation humaine")."""
        return self.level < RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP


_UNKNOWN = IncidentClassification(
    IncidentCategory.CODE_LOGIC_UNKNOWN, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
    "no known classification for this incident type -- default to the safest outcome (SAFE STOP -> REPORT -> WAIT) "
    "rather than guess; per item 9, an unrecognized problem becomes CODE_FIX_REQUIRED, never an auto-patch",
)

# (stable_substring, classification) -- checked top to bottom, first
# match wins. Each entry cites the real incident type string(s) it
# matches, taken verbatim from the codebase.
_RULES: list[tuple[str, IncidentClassification]] = [
    # --- CRITICAL_SAFETY: always LEVEL 5, always GLOBAL -- these are
    # the exact items on the user's own item-6 LEVEL 5 list, and every
    # one of them means real, unhedged, or unexplained money risk.
    ("API PERMISSION CHANGE", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "withdrawal permission or spot-trading permission changed unexpectedly -- never auto-resolvable",
    )),
    ("NEUTRALIZATION FAILED", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "a real unhedged position exists -- on the user's own critical list verbatim",
    )),
    ("COULD NOT RESTORE THE FLOOR", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "a real rebalance sell executed but still failed to restore the reserve floor -- 'reserve floor impossible "
        "a restaurer' is on the user's own critical list verbatim",
    )),
    ("CAPITAL SAFETY VIOLATION", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "a real trade notional exceeded its authorized cap -- on the user's own critical list verbatim",
    )),
    ("SESSION LOSS LIMIT REACHED", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "the real-money session-loss circuit breaker fired exactly as designed -- an intentional, successful risk "
        "control, not a bug, but still ends the session (never auto-restarted by this layer)",
    )),
    ("FEE ASSET UNKNOWN", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "a real fee was paid in an asset this system cannot price -- never invent a USD-equivalent to keep going",
    )),
    ("UNEXPLAINED P&L DISCREPANCY", IncidentClassification(
        IncidentCategory.CODE_LOGIC_UNKNOWN, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "actual P&L diverged from the predicted figure by more than the accepted threshold on a fully reconciled "
        "trade -- historically (this project) every prior instance of this shape was a real accounting bug, so it "
        "is always routed to CODE_FIX_REQUIRED, never guessed at",
    )),
    ("DOUBLE ALLOCATION", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "two concurrent operations claimed the same capital -- on the user's own critical list verbatim",
    )),
    ("KILL SWITCH ENGAGED", IncidentClassification(
        IncidentCategory.CRITICAL_SAFETY, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "a LiveTradingGuard/InventoryConstitutionGuard kill switch latched -- this flag is permanent for the "
        "life of that guard instance, never a recalculate-and-continue case even when the same incident string "
        "also mentions an unknown order state; checked before the UNKNOWN ORDER STATE rule below on purpose so "
        "the combined real string 'UNKNOWN ORDER STATE / KILL SWITCH ENGAGED (inventory leg)' resolves here",
    )),
    # --- AMBIGUOUS_ORDER_STATE: the specific, safe recovery is exactly
    # ONE more authoritative real-order-status READ (never a blind
    # resubmission of the order itself) -- see recovery_actions.py. Only
    # escalates to LEVEL 5 if that single fresh read is also
    # inconclusive.
    ("UNKNOWN ORDER STATE", IncidentClassification(
        IncidentCategory.AMBIGUOUS_ORDER_STATE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION,
        "a real order's terminal status could not be confirmed within the poll window -- attempt exactly one more "
        "authoritative status/trades re-read (a GET, never a new order) before treating this as unresolvable",
    )),
    ("AMBIGUOUS SUBMISSION ERROR", IncidentClassification(
        IncidentCategory.AMBIGUOUS_ORDER_STATE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION,
        "the order submission call itself raised, so a real order may or may not exist -- never blind-retry "
        "submission; attempt one authoritative status read for that client_order_id first",
    )),
    # --- RECONCILIATION: attempt reconciliation self-healing (re-fetch
    # balances/trades, try known-but-not-yet-integrated event types)
    # before treating a mismatch as unexplained.
    ("BALANCE / LEDGER MISMATCH", IncidentClassification(
        IncidentCategory.RECONCILIATION, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION,
        "attempt reconciliation self-healing first (see reconciliation_self_healing.py) -- only a mismatch that "
        "remains unexplained after that becomes a real SAFE STOP",
    )),
    # --- ORDER_REJECTED_SAFE: the buy filled, the sell was rejected,
    # but neutralization SUCCEEDED -- the position is flat and safe.
    # This is exactly the "un probleme RVN ne doit pas arreter ZIL"
    # case: isolate the symbol+direction, keep scanning everything else.
    ("SELL REJECTED AFTER BUY", IncidentClassification(
        IncidentCategory.ORDER_REJECTED_SAFE, RecoveryLevel.LEVEL_3_ISOLATE_OPPORTUNITY, RecoveryScope.SYMBOL_DIRECTION,
        "the sell leg was rejected but neutralization successfully flattened the buy -- no unhedged exposure, so "
        "only this symbol+direction needs to pause, not the whole session",
    )),
    # --- CAPITAL_REBALANCE: a rebalance attempt failed for a
    # recoverable, non-ambiguous reason (no fresh market data, this
    # specific asset isn't tradable right now, insufficient balance in
    # THIS asset). Recalculate against a different reconvertible
    # position or just retry next scan -- never immediately global.
    ("REBALANCE FAILED", IncidentClassification(
        IncidentCategory.CAPITAL_REBALANCE, RecoveryLevel.LEVEL_2_RECALCULATE, RecoveryScope.SYMBOL_DIRECTION,
        "a specific rebalance attempt failed for a safely-detected (non-ambiguous) reason -- retry with a "
        "different reconvertible position or defer to the next scan; this is a normal, expected operational event",
    )),
    # --- RECOVERABLE_OPERATIONAL: exhausted the built-in read-only
    # retry budget. No order was ever submitted for these (they are all
    # scan/quote/balance reads), so isolating with a cooldown is safe;
    # escalate to GLOBAL only if isolating the exchange doesn't apply
    # (recovery_actions.py decides that from context).
    ("REPEATED EXECUTION ERROR", IncidentClassification(
        IncidentCategory.RECOVERABLE_OPERATIONAL, RecoveryLevel.LEVEL_4_ISOLATE_EXCHANGE, RecoveryScope.EXCHANGE,
        "read-only errors (scans/quotes/balance reads, never an order) exceeded the retry budget -- no capital is "
        "at risk, so pause with a cooldown and revalidate rather than a global stop",
    )),
    # --- CODE_LOGIC_UNKNOWN: an unrecognized raw exception during a
    # real order/inventory/rebalance step. Default is LEVEL 5 (matches
    # the user's own "nouveau bug logique non classifie" critical-list
    # item) -- recovery_actions.py may downgrade this only when the
    # underlying exception is independently verified to be a plain
    # pre-submission network error (see incident_knowledge_base.py).
    ("SCRIPT ERROR", IncidentClassification(
        IncidentCategory.CODE_LOGIC_UNKNOWN, RecoveryLevel.LEVEL_5_GLOBAL_SAFE_STOP, RecoveryScope.GLOBAL,
        "an unrecognized exception occurred during a real step -- default to CODE_FIX_REQUIRED unless the "
        "exception is independently known-safe (see recovery_actions.classify_script_error)",
    )),
]


def classify_incident(incident_type: str) -> IncidentClassification:
    """Pure function. `incident_type` is the exact `incident["type"]`
    string this codebase's real executors/orchestrators already
    produce. Returns the safest applicable classification; an
    unrecognized string is never silently assumed benign."""
    for substring, classification in _RULES:
        if substring in incident_type:
            return classification
    return _UNKNOWN
