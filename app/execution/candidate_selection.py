"""CANDIDATE SELECTION / CLASSIFICATION (user directive, 2026-08-24, after
the 0/5 real-cycle batch burned all 40 attempts re-selecting the same
RVN candidate). RVN's real Bybit inventory (84.5821 units, ~0.27 USDT)
cleared MIN_QTY but not MIN_NOTIONAL -- the batch orchestrator's pre-check
only verified the former, so it kept re-picking RVN by edge alone and
let execute_one_arbitrage's own (correct) internal check reject it,
41 -- er, 40 -- times running, instead of trying ZIL/SAND/LUNC/etc.

Pure, testable, no network I/O and no order placement anywhere in this
module -- classify_candidate() and select_best_candidate() only judge
data the caller already fetched fresh; CandidateRejectionCache only
tracks what has already been judged this session. Every real fetch and
every real order still goes through the existing, separately-tested
app.execution.inventory_constitution_executor / live_arbitrage_executor.

Of the 8 required statuses, EDGE_DISAPPEARED is never returned directly
by classify_candidate -- it is a caller-side relabeling of EDGE_TOO_LOW
specifically for a re-check performed AFTER a successful inventory
constitution (a temporal distinction classify_candidate's single fresh
snapshot has no way to know on its own)."""

import time
from dataclasses import dataclass, field
from enum import StrEnum

from app.execution.dual_leg_quote import compute_common_dual_leg_qty


class CandidateStatus(StrEnum):
    EXECUTABLE_NOW = "EXECUTABLE_NOW"
    INVENTORY_MISSING = "INVENTORY_MISSING"
    BELOW_MIN_NOTIONAL = "BELOW_MIN_NOTIONAL"
    INSUFFICIENT_BALANCE = "INSUFFICIENT_BALANCE"
    INSUFFICIENT_DEPTH = "INSUFFICIENT_DEPTH"
    EDGE_TOO_LOW = "EDGE_TOO_LOW"
    EDGE_DISAPPEARED = "EDGE_DISAPPEARED"
    OTHER = "OTHER"


# select_best_candidate() treats these two -- and only these two -- as
# legitimate to act on: EXECUTABLE_NOW needs nothing further,
# INVENTORY_MISSING is eligible for automatic inventory constitution
# (Mission 4). Every other status must never reach execute_one_arbitrage
# or constitute_inventory.
SELECTABLE_STATUSES = (CandidateStatus.EXECUTABLE_NOW, CandidateStatus.INVENTORY_MISSING)


@dataclass(slots=True)
class CandidateClassification:
    status: CandidateStatus
    reason: str
    common_qty: float  # 0.0 unless status is EXECUTABLE_NOW or (rarely) a partially-computed value; always what a caller may safely act on


def classify_candidate(
    *,
    quote_executable: bool,
    quote_net_profit_usd: float,
    quote_executable_qty: float,
    quote_reason: str | None,
    safety_margin_usd: float,
    buy_price: float,
    sell_price: float,
    buy_available_usdt: float,
    sell_available_qty: float,
    buy_min_qty: float,
    sell_min_qty: float,
    buy_min_notional: float,
    sell_min_notional: float,
    buy_step_size: float,
    sell_step_size: float,
) -> CandidateClassification:
    """Pure function. All inputs are real, freshly-fetched values (order
    book, exchange filters, account balances) -- never hardcoded
    thresholds. Mirrors the exact checklist from the user's directive,
    in the order a real evaluation naturally resolves them: is the
    reference-size quote even tradable (depth) -> is it profitable
    enough (edge) -> is there any inventory to sell at all -> does the
    REAL, balance-constrained common_qty still clear both exchanges'
    min_qty/min_notional -> is there enough buy-side capital.

    common_qty is computed here via the same compute_common_dual_leg_qty
    already used (and tested) inside live_arbitrage_executor -- this
    function does not re-implement that rounding, only classifies its
    result against real exchange filters that were never checked at
    this (possibly much smaller than reference-size) quantity before."""
    if not quote_executable:
        return CandidateClassification(CandidateStatus.INSUFFICIENT_DEPTH, quote_reason or "quote not executable (depth/tradability check failed)", 0.0)

    if quote_net_profit_usd <= safety_margin_usd:
        return CandidateClassification(
            CandidateStatus.EDGE_TOO_LOW,
            f"net_profit_usd={quote_net_profit_usd} does not clear safety_margin_usd={safety_margin_usd}",
            0.0,
        )

    if sell_available_qty <= 0:
        return CandidateClassification(CandidateStatus.INVENTORY_MISSING, "sell exchange holds none of the base asset", 0.0)

    if quote_executable_qty <= 0:
        return CandidateClassification(CandidateStatus.OTHER, "quote_executable_qty<=0 despite an executable, profitable quote -- unexpected", 0.0)

    common_qty = compute_common_dual_leg_qty(quote_executable_qty, sell_available_qty, buy_step_size, sell_step_size)
    if common_qty <= 0:
        return CandidateClassification(CandidateStatus.INVENTORY_MISSING, f"no common quantity after step rounding (sell_available_qty={sell_available_qty})", 0.0)

    if common_qty < buy_min_qty or common_qty < sell_min_qty:
        return CandidateClassification(
            CandidateStatus.BELOW_MIN_NOTIONAL,
            f"common_qty={common_qty} below min_qty (buy_min_qty={buy_min_qty}, sell_min_qty={sell_min_qty})",
            common_qty,
        )

    buy_notional = common_qty * buy_price
    sell_notional = common_qty * sell_price
    if buy_notional < buy_min_notional or sell_notional < sell_min_notional:
        return CandidateClassification(
            CandidateStatus.BELOW_MIN_NOTIONAL,
            f"common_qty={common_qty}: buy_notional={buy_notional} (min {buy_min_notional}), sell_notional={sell_notional} (min {sell_min_notional})",
            common_qty,
        )

    required_buy_usdt = common_qty * buy_price
    if buy_available_usdt < required_buy_usdt:
        return CandidateClassification(
            CandidateStatus.INSUFFICIENT_BALANCE,
            f"buy_available_usdt={buy_available_usdt} < required {required_buy_usdt}",
            common_qty,
        )

    return CandidateClassification(CandidateStatus.EXECUTABLE_NOW, "passes all pre-checks", common_qty)


@dataclass(slots=True)
class TopupPlan:
    should_topup: bool
    topup_notional_usdt: float
    reason: str


def compute_inventory_topup_plan(
    *,
    classification_status: CandidateStatus,
    sell_available_qty: float,
    sell_price: float,
    sell_min_notional: float,
    max_topup_usdt: float,
) -> TopupPlan:
    """Pure function (user directive, 2026-08-24, INVENTORY RECYCLING /
    item 5): after one cycle, a symbol's sell-side residual routinely
    falls below MIN_NOTIONAL (dust) without hitting zero -- classify_
    candidate correctly reports BELOW_MIN_NOTIONAL for that, but nothing
    previously let the batch top the position back up rather than
    treating it as permanently spent for the rest of the session.

    Only ever applies to an ALREADY-BELOW_MIN_NOTIONAL classification --
    never overrides any other status, and is not itself a fresh edge
    check: the caller must still re-run classify_candidate against the
    real combined balance after any actual top-up fill before treating
    the symbol as executable again (constitute_inventory does its own
    post-fill edge revalidation regardless). The requested top-up is
    only ever the real shortfall to a modest target above the floor
    (1.5x MIN_NOTIONAL, so the position does not immediately dip back
    under it after the very next sell) -- existing dust always counts
    toward that target, this never blindly requests the full cap."""
    if classification_status != CandidateStatus.BELOW_MIN_NOTIONAL:
        return TopupPlan(False, 0.0, "not applicable -- classification is not BELOW_MIN_NOTIONAL")
    if sell_price <= 0 or sell_min_notional <= 0:
        return TopupPlan(False, 0.0, "no valid price/min_notional to plan a top-up against")

    current_value_usdt = sell_available_qty * sell_price
    target_notional_usdt = sell_min_notional * 1.5
    shortfall_usdt = target_notional_usdt - current_value_usdt
    if shortfall_usdt <= 0:
        return TopupPlan(
            False, 0.0,
            f"current dust ({current_value_usdt} USDT) already at/above the target ({target_notional_usdt} USDT) -- "
            "BELOW_MIN_NOTIONAL must be from a buy-side balance or common-qty constraint, not sell-side dust",
        )
    if shortfall_usdt > max_topup_usdt:
        return TopupPlan(False, 0.0, f"shortfall {shortfall_usdt} USDT exceeds max_topup_usdt cap {max_topup_usdt} USDT")
    return TopupPlan(
        True, round(shortfall_usdt, 2),
        f"topping up {round(shortfall_usdt, 2)} USDT to reach target {target_notional_usdt} USDT (current dust worth {current_value_usdt} USDT)",
    )


@dataclass(slots=True)
class CandidateInput:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    classification: CandidateClassification


def select_best_candidate(ranked_candidates: list[CandidateInput]) -> CandidateInput | None:
    """Pure function. ranked_candidates must already be sorted by the
    caller's own priority (e.g. real, currently-executable net profit
    first -- never bare theoretical edge alone, per the user's Mission 3:
    "classe d'abord les candidats par profit net reellement executable").
    Returns the first candidate whose classification is SELECTABLE
    (EXECUTABLE_NOW, or INVENTORY_MISSING for automatic constitution) --
    every BELOW_MIN_NOTIONAL / INSUFFICIENT_* / EDGE_* / OTHER candidate
    is skipped, never selected, regardless of how good its edge looked."""
    for c in ranked_candidates:
        if c.classification.status in SELECTABLE_STATUSES:
            return c
    return None


@dataclass(slots=True)
class _CacheEntry:
    signature: dict = field(default_factory=dict)
    rejected_at: float = 0.0


class CandidateRejectionCache:
    """Session-scoped (in-memory only, never persisted, never shared
    across processes) cache preventing a batch orchestrator from
    re-evaluating -- and re-selecting -- the exact same structurally
    rejected candidate over and over with no new information. This is
    what the 2026-08-24 bug was actually missing: execute_one_arbitrage
    correctly rejected the same doomed RVN attempt every single time,
    but nothing remembered that rejection between attempts.

    Keyed by (symbol, direction, rejection_reason) -- the user's own
    minimal key. A cached rejection is trusted only while the caller-
    supplied signature (whatever inputs could have changed the verdict:
    inventory balance, regime, edge, price -- entirely the caller's
    choice what to include) is IDENTICAL to what produced it, and only
    within ttl_seconds. Any change in the signature, or the TTL
    expiring, means the candidate must be freshly re-evaluated -- this
    cache only ever suppresses a *provably unchanged* re-rejection, it
    never suppresses a candidate outright."""

    def __init__(self, ttl_seconds: float = 60.0) -> None:
        self._ttl_seconds = ttl_seconds
        self._entries: dict[tuple[str, str, str], _CacheEntry] = {}

    @staticmethod
    def _key(symbol: str, direction: str, reason: str) -> tuple[str, str, str]:
        return (symbol, direction, reason)

    def is_still_valid_rejection(self, symbol: str, direction: str, reason: str, signature: dict, now: float | None = None) -> bool:
        entry = self._entries.get(self._key(symbol, direction, reason))
        if entry is None:
            return False
        now = now if now is not None else time.time()
        if now - entry.rejected_at > self._ttl_seconds:
            return False
        return entry.signature == signature

    def record_rejection(self, symbol: str, direction: str, reason: str, signature: dict, now: float | None = None) -> None:
        self._entries[self._key(symbol, direction, reason)] = _CacheEntry(signature=dict(signature), rejected_at=now if now is not None else time.time())

    def clear(self, symbol: str, direction: str) -> None:
        """Drop every cached rejection for this (symbol, direction) --
        call after any action that could have changed its executability
        (e.g. a successful inventory constitution on it)."""
        for key in [k for k in self._entries if k[0] == symbol and k[1] == direction]:
            del self._entries[key]
