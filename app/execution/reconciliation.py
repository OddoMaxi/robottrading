"""BALANCE RECONCILIATION (user directive 2026-08-24 FIX 2; 2026-08-25
FIX 3; rebuilt 2026-08-25 FIX 4 -- MULTI-ASSET RECONCILIATION). Verifies
a real, observed per-(exchange, asset) balance delta against every real
ledger event that could have touched it.

FIX 4's reason for existing: FIX 3 added a `rebalance_sell_exchange`/
`rebalance_sell_qty` PAIR OF SCALARS to the old keyword-argument design,
keyed only by exchange. The first real CONTINUOUS LIVE V3 session then
hit exactly the flaw that shape invites: a rebalance sold RVN on Binance
to fund a ZIL arbitrage, and because the old function only checked
"did *a* rebalance happen on *this exchange*", it subtracted the RVN
quantity from the ZIL reconciliation -- a false 2107.9-unit mismatch
that halted the session, even though both the rebalance and the ZIL
arbitrage were individually correct.

FIX 4 replaces the fixed set of named optional parameters with a flat
list of explicitly-typed LedgerEvent records, each carrying its OWN
`base_asset`. reconcile_asset_balance() filters events to
`exchange == exchange AND base_asset == asset` BEFORE summing anything
-- an event for a different asset contributes exactly zero, structurally,
regardless of how the caller assembled the event list. This makes the
whole bug CLASS (not just this one instance) impossible: there is no
longer any parameter whose meaning is "this exchange had SOME event",
divorced from which asset it was.

Pure functions, no I/O, no order placement -- callers assemble
LedgerEvent records from data app.execution.inventory_constitution_executor
/ live_arbitrage_executor / the capital rebalancer already produced and
persisted."""

from dataclasses import dataclass
from enum import StrEnum


class LedgerEventType(StrEnum):
    ARBITRAGE_BUY = "ARBITRAGE_BUY"
    ARBITRAGE_SELL = "ARBITRAGE_SELL"
    INVENTORY_CONSTITUTION = "INVENTORY_CONSTITUTION"  # covers both fresh constitution and recycling -- identical ledger effect (net base-asset qty added on one exchange); the business-level distinction lives in the caller's own inventory_action label, not in this event's type
    REBALANCE_SELL = "REBALANCE_SELL"
    NEUTRALIZATION = "NEUTRALIZATION"


@dataclass(slots=True, frozen=True)
class LedgerEvent:
    """One real, explicitly-typed movement. `net_base_delta` is what
    this module sums for reconciliation -- the actual effect on the
    (exchange, base_asset) wallet balance, already fee-aware (BUY-shaped
    events: gross fill minus a same-asset fee, matching
    net_base_qty_after_fee's existing semantics; SELL-shaped events: the
    full filled/sold quantity, since a trading fee is conventionally
    deducted from what you RECEIVE in a trade -- quote currency for a
    sell -- not from the base asset you are giving up). `gross_base_delta`
    is kept only as audit metadata and is never summed.
    `quote_delta`/`fee_asset`/`fee_amount` exist for completeness and for
    REBALANCING_PNL/audit purposes -- this module does not itself
    reconcile the quote asset."""

    event_type: LedgerEventType
    exchange: str
    base_asset: str
    quote_asset: str
    side: str  # "BUY" | "SELL"
    gross_base_delta: float
    net_base_delta: float
    quote_delta: float | None
    fee_asset: str | None
    fee_amount: float | None
    order_id: str | None
    timestamp: str | None


@dataclass(slots=True, frozen=True)
class AssetReconciliationResult:
    exchange: str
    asset: str
    expected_delta: float
    actual_delta: float
    difference: float
    tolerance: float
    match: bool
    contributing_events: tuple[LedgerEvent, ...]
    explanation: str


def reconcile_asset_balance(
    *,
    exchange: str,
    asset: str,
    before_balance: float,
    after_balance: float,
    events: tuple[LedgerEvent, ...] = (),
    tolerance_abs: float = 0.05,
    tolerance_rel: float = 0.02,
) -> AssetReconciliationResult:
    """Pure function. Filters `events` to exactly this (exchange, asset)
    pair before summing `net_base_delta` -- an event for a different
    asset, or a different exchange, contributes zero by construction,
    never by caller discipline. tolerance is the larger of tolerance_abs
    and tolerance_rel * |expected_delta| -- a small, disclosed allowance
    for dust/rounding, never a silent excuse for an unexplained gap."""
    contributing = tuple(e for e in events if e.exchange == exchange and e.base_asset == asset)
    expected_delta = sum(e.net_base_delta for e in contributing)

    actual_delta = after_balance - before_balance
    difference = actual_delta - expected_delta
    tolerance = max(tolerance_abs, abs(expected_delta) * tolerance_rel)
    match = abs(difference) <= tolerance

    parts = [f"{'+' if e.net_base_delta >= 0 else ''}{e.net_base_delta} ({e.event_type.value})" for e in contributing]
    explanation = (
        f"exchange={exchange} asset={asset}: expected_delta={expected_delta} ({', '.join(parts) if parts else 'no events for this (exchange, asset)'}), "
        f"actual_delta={actual_delta} (before={before_balance}, after={after_balance}), "
        f"difference={difference}, tolerance={tolerance} -> {'MATCH' if match else 'MISMATCH'}"
    )
    return AssetReconciliationResult(
        exchange=exchange, asset=asset, expected_delta=expected_delta, actual_delta=actual_delta,
        difference=difference, tolerance=tolerance, match=match, contributing_events=contributing, explanation=explanation,
    )
