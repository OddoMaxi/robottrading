"""INCIDENT KNOWLEDGE BASE (user directive, 2026-08-25, AUTONOMOUS
SELF-HEALING OPERATIONS LAYER, item 4). A durable, JSON-backed catalog of
real incidents this project has already root-caused and fixed, keyed by
a short stable `incident_signature` -- when the same signature reappears,
the layer applies the already-validated `safe_recovery` directly instead
of re-deriving it (or worse, stopping and asking a human again).

SEED_KNOWN_INCIDENTS below is grounded in this project's real git
history -- every entry cites the actual commit that fixed it, not an
invented example. Two entries (RECONCILIATION_MISSING_REBALANCE_EVENT,
RESERVE_FLOOR_DEPLETION) are from 2026-08-25's own capital-rebalancer
integration work.

Pure core (lookup_known_incident / record_occurrence / add_resolved_incident
operate on a plain dict, no I/O); load_state/save_state are the only I/O,
isolated at the edges."""

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

DEFAULT_STATE_PATH = Path("/opt/robotcripto/data/incident_knowledge_base_state.json")


@dataclass(slots=True, frozen=True)
class KnownIncident:
    incident_signature: str
    root_cause: str
    safe_recovery: str
    validation: str
    first_seen: str  # ISO date
    last_seen: str  # ISO date
    occurrence_count: int


SEED_KNOWN_INCIDENTS: tuple[KnownIncident, ...] = (
    KnownIncident(
        incident_signature="BYBIT_ORDER_LINK_ID_TOO_LONG",
        root_cause="Bybit's orderLinkId has a documented 36-character max; ids built from a full uuid4() plus a "
        "descriptive prefix/suffix ('inventory-{uuid4}', 'live-{uuid4}-buy/-sell', 'neutralize-{uuid4}') ran "
        "45-47 characters and were rejected with retCode=170003 'An unknown parameter was sent' (retExtInfo empty, "
        "no field-level detail).",
        safe_recovery="Generate every client_order_id/order_link_id as a short fixed prefix + uuid4().hex[:24] "
        "(no hyphens) -- 28-29 chars total, comfortably under the limit, still effectively collision-proof at "
        "max_concurrent=1.",
        validation="Regression tests assert every generated id stays <= 36 chars (commit b3e808d, 2026-08-24). "
        "Zero recurrences since.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="BYBIT_BUY_QUOTE_BASE_COIN_MISMATCH",
        root_cause="Bybit v5 Spot market orders require an explicit marketUnit ('quoteCoin' for Buy, 'baseCoin' "
        "for Sell) plus isLeverage=0 and orderFilter='Order'; without it a real order was rejected with "
        "retCode=170003. This also changes what qty MEANS for a Buy (USDT notional, not a pre-estimated base-"
        "asset quantity) -- callers that converted notional to an estimated base qty via a book price first were "
        "silently passing the wrong number.",
        safe_recovery="place_market_order sends the explicit marketUnit/isLeverage/orderFilter fields; callers "
        "pass the raw USDT notional straight through for Bybit buys instead of pre-converting it.",
        validation="Fixed in commit 4a1e1b0 (2026-08-24), verified against real Bybit v5 API docs. Zero "
        "recurrences since.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="BINANCE_FEE_REQUIRES_MYTRADES",
        root_cause="Binance's order-status endpoint (get_order_status) never returns fill/commission detail -- only "
        "the account trade-history endpoint (myTrades) does. Code that read fees off get_order_status silently reported fee_asset=None, "
        "hiding real base-asset fees (this recurred twice: once in live_arbitrage_executor.py, commit 869fd26, "
        "then again in inventory_constitution_executor.py's own separate Binance branch, which had the same bug "
        "independently).",
        safe_recovery="After confirming an order is terminal and filled via get_order_status, call "
        "get_order_trades(symbol, order_id) (GET /api/v3/myTrades) + aggregate_binance_trades() to get real "
        "per-fill commission/commissionAsset/price/qty, then resolve_fee() the same way Bybit's cumFeeDetail "
        "already is. Never trust get_order_status alone for fee data.",
        validation="Fixed commits 869fd26 (2026-08-24, live_arbitrage_executor.py) and dd65801 (2026-08-24, "
        "inventory_constitution_executor.py, found via a real SAND cycle's false reconcile mismatch). Both "
        "call sites now use the same pattern; a grep-based isolation test guards the get_order_status call site "
        "against reintroducing an unfee'd read.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=2,
    ),
    KnownIncident(
        incident_signature="FEE_CURRENCY_ASSUMED_USDT",
        root_cause="A real Bybit fill reported cum_exec_fee=2.9179 which was blindly treated as USDT (a ~29% "
        "'fee' on a normal trade) -- the actual fee was charged in the base asset (~$0.01, a normal ~0.1% fee), "
        "visible in Bybit's own cumFeeDetail but never read.",
        safe_recovery="resolve_fee()/net_base_qty_after_fee() read the real fee asset from cumFeeDetail "
        "(Bybit) or myTrades commissionAsset (Binance) instead of assuming USDT, compute a USD equivalent only "
        "when the asset is recognized (this symbol's own base or quote), and compute the NET base-asset quantity "
        "actually held after the fill.",
        validation="Fixed commit 1caeb75 (2026-08-24). fee_asset/fee_amount/*_usd_equivalent columns added to "
        "both ledger tables; every real fee since has resolved to the correct asset.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="MIN_NOTIONAL_CHECKED_LATE_OR_NOT_AT_ALL",
        root_cause="The original candidate selector checked MIN_QTY but never MIN_NOTIONAL before attempting a "
        "trade, so a candidate whose common_qty cleared the quantity floor but not the notional floor was "
        "retried 40/40 times against the exact same doomed BELOW_MIN_NOTIONAL RVN candidate, wasting the entire "
        "first validation batch with zero real orders placed.",
        safe_recovery="classify_candidate() checks depth -> edge -> inventory-zero -> common_qty -> min_qty -> "
        "min_notional -> buy_balance in a fixed order, BEFORE any order is submitted, and CandidateRejectionCache "
        "avoids re-evaluating a candidate whose rejection reason and underlying signature (balance/regime/edge) "
        "haven't changed since it was last rejected.",
        validation="Fixed commit e8eea0d (2026-08-24). The next validation batch achieved 3/5 real successes "
        "with zero wasted attempts on a pre-known-doomed candidate.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="ARBITRAGE_SELL_QTY_EXCEEDS_SELL_SIDE_INVENTORY",
        root_cause="The first real arbitrage attempt bought 3003.5 RVN on Binance (sized purely by USDT "
        "notional) then tried to sell that same 3003.5 on Bybit, which only held 2914.9821 real inventory -- "
        "rejected, because nothing had ever bounded the buy leg by the sell exchange's own real balance.",
        safe_recovery="compute_common_dual_leg_qty() computes min(price/depth-aware executable_qty, real "
        "sell-exchange inventory) BEFORE ever submitting the buy order, floored to a step size valid on both "
        "exchanges -- the buy leg is quantity-capped, not notional-capped. After the real buy fill, the sell "
        "quantity is recomputed as min(actual net buy qty, a FRESH real sell-exchange balance read, the "
        "pre-trade ceiling) and revalidated net-positive before submitting.",
        validation="Fixed commit 869fd26 (2026-08-24), with regression tests reproducing the exact real numbers "
        "(2914.9821 Bybit inventory, 3003.5 gross buy fill). Zero oversell recurrences since.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="NEUTRALIZATION_QTY_EXCEEDS_FREE_BALANCE",
        root_cause="The automatic neutralization after the above oversell attempt tried to sell back 3003.5 RVN "
        "on Binance, when only ~3000.4965 was actually held (an untracked ~3 RVN fee) -- itself rejected. Real "
        "loss from manually unwinding it: -$0.05.",
        safe_recovery="_neutralize() always re-reads the real free balance immediately before submitting and "
        "caps to it (floored to step size, with one step of technical reserve), rather than trusting a "
        "caller-supplied estimated quantity.",
        validation="Fixed commit 869fd26 (2026-08-24), same commit as the sell-qty fix (both root causes were "
        "part of the same incident). Zero recurrences since.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="BUY_EXCHANGE_USDT_RESERVE_NOT_CHECKED",
        root_cause="10 consecutive same-direction (Binance-buy/Bybit-sell) RVN recycling cycles drained Binance "
        "USDT from ~72.79 to 2.66 over a real continuous-live session; the common-dual-leg-sizing fix above only "
        "ever bounded the trade by SELL-exchange base-asset inventory, never by the BUY-exchange's own USDT "
        "reserve, so nothing ever warned the session it was about to run the buy exchange dry until a real BUY "
        "order was rejected outright.",
        safe_recovery="Before every real USDT-spending action, decide_trade_with_reserve_check() compares the "
        "buy exchange's real USDT against its reserve floor (compute_reserve_floor = clamp(2.5x max notional "
        "per leg, 20, 25)); if the trade would breach it, prefer a profitable opposite-direction trade, else "
        "REBALANCE_FIRST (sell the minimal amount of existing same-exchange inventory back to USDT), else "
        "DO_NOT_TRADE. Never breach the floor to force a trade through.",
        validation="Root-caused and fixed 2026-08-25 (app.execution.capital_rebalancer). 31-event replay of the "
        "exact real incident sequence proves the floor is never meaningfully breached (min 24.99 vs the real "
        "observed 2.66 USDT), 32+5 tests passing.",
        first_seen="2026-08-24", last_seen="2026-08-25", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="RECONCILIATION_MISSING_REBALANCE_EVENT",
        root_cause="reconcile_base_asset_balance() predates the capital rebalancer and had no term for a "
        "REBALANCE_FIRST sell happening on the same exchange earlier in the same cycle. A real cycle sold 2192.5 "
        "RVN via REBALANCE_FIRST on Binance, then bought back net 2125.1727 RVN there via the arbitrage leg; "
        "reconciliation expected only the +2125.1727 buy and flagged a false -2192.5 'BALANCE / LEDGER MISMATCH' "
        "that halted the very first CONTINUOUS LIVE V2 session after one successful cycle.",
        safe_recovery="reconcile_base_asset_balance() gained rebalance_sell_exchange/rebalance_sell_qty "
        "parameters, symmetric to inventory_constitution/arbitrage_buy/arbitrage_sell/neutralization -- a "
        "rebalance sell always subtracts on the exchange it happened on, exactly like a neutralization.",
        validation="Fixed 2026-08-25, verified independently three ways before the fix was written (script log "
        "arithmetic, a fresh separate balance re-read, and raw Binance myTrades pulled directly for orders "
        "1344692249/1344692270) -- all three agreed to the decimal that no RVN was actually missing. Replay test "
        "reproduces the exact real incident and proves it now reconciles. Superseded 2026-08-25 by "
        "CROSS_ASSET_LEDGER_CONTAMINATION below (FIX 4), which generalizes this fix's own shape -- the "
        "rebalance_sell_exchange/qty parameters this fix added were themselves keyed by exchange only, not by "
        "asset, and that gap is exactly what FIX 4 closes.",
        first_seen="2026-08-25", last_seen="2026-08-25", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="CROSS_ASSET_LEDGER_CONTAMINATION",
        root_cause="FIX 3's own rebalance_sell_exchange/rebalance_sell_qty parameters were keyed by exchange "
        "only, not by (exchange, asset) -- so any event described as 'a rebalance happened on this exchange' got "
        "applied to whichever asset the CALLER happened to be reconciling, regardless of which asset the rebalance "
        "actually sold. The first real CONTINUOUS LIVE V3 session hit this immediately: REBALANCE_FIRST sold "
        "2107.9 RVN on Binance to fund a ZIL arbitrage buy (net 2626.2711 ZIL); both the rebalance and the ZIL "
        "arbitrage were individually correct, but reconciliation subtracted the RVN quantity from the ZIL check, "
        "producing a false 2107.9-unit 'BALANCE / LEDGER MISMATCH' that halted the session after 3 profitable "
        "cycles. Independently verified safe: a fresh balance re-read matched the script's own final numbers "
        "exactly, and the real ZIL delta (before=6202.0917, after=8828.3628) matched the arbitrage's own net buy "
        "fill (2626.2711) exactly once the RVN event was excluded.",
        safe_recovery="Replaced the fixed set of named optional parameters with a flat list of explicitly-typed "
        "LedgerEvent records, each carrying its OWN base_asset (app.execution.reconciliation.reconcile_asset_"
        "balance). Reconciliation now filters events to exchange==exchange AND base_asset==asset BEFORE summing "
        "anything -- an event for a different asset contributes exactly zero, structurally, regardless of how the "
        "caller assembled the event list. The self-healing layer additionally detects and explicitly refuses any "
        "candidate event whose asset does not match (CROSS_ASSET_RECONCILIATION_ATTEMPT), never using it as an "
        "explanation even when its magnitude would numerically close the gap.",
        validation="Fixed 2026-08-25. Exact live incident replay "
        "(tests/test_reconciliation.py::test_zil_rvn_live_incident_exact_replay_reconciles_cleanly) proves ZIL "
        "reconciles cleanly with the RVN rebalance event contributing zero; a generic cross-asset-contamination "
        "test (4 distinct assets: rebalance/arbitrage/inventory/neutralization) and a multi-rebalance test (3 "
        "chained rebalance->arbitrage pairs sharing an asset) both prove no event ever leaks into another asset's "
        "ledger; self-healing tests prove a cross-asset candidate is rejected and reported even when it would "
        "numerically fit.",
        first_seen="2026-08-25", last_seen="2026-08-25", occurrence_count=1,
    ),
    KnownIncident(
        incident_signature="ORDER_LINK_ID_OR_CLIENT_ORDER_ID_COLLISION_RISK",
        root_cause="A generic risk class rather than one specific incident: any exchange order-id field reused "
        "across concurrent or retried submissions risks the exchange treating a resubmission as a duplicate (or "
        "worse, ambiguously matching the wrong order) -- relevant whenever a retry path exists.",
        safe_recovery="Always mint a fresh client_order_id/order_link_id per submission attempt (uuid4-derived, "
        "never reused across retries) and never resubmit under the same id after an ambiguous result -- treat a "
        "retry as a brand-new order with its own id, gated by AMBIGUOUS_ORDER_STATE handling first.",
        validation="Structural convention followed by every real order path in this codebase "
        "(binance_live_trade_client.py, bybit_live_trade_client.py, continuous_live_session_v2/v3's own "
        "client_order_id generation) since commit b3e808d (2026-08-24). No collision observed in any real "
        "session.",
        first_seen="2026-08-24", last_seen="2026-08-24", occurrence_count=1,
    ),
)


def load_state(path: Path = DEFAULT_STATE_PATH) -> dict[str, KnownIncident]:
    """Loads the persisted KB, seeding it with SEED_KNOWN_INCIDENTS the
    first time (file doesn't exist yet) so a fresh deployment already
    knows every incident this project has already solved."""
    if not path.exists():
        return {k.incident_signature: k for k in SEED_KNOWN_INCIDENTS}
    with open(path) as f:
        raw = json.load(f)
    return {sig: KnownIncident(**entry) for sig, entry in raw.items()}


def save_state(state: dict[str, KnownIncident], path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({sig: asdict(k) for sig, k in state.items()}, f, indent=2, sort_keys=True)


def lookup_known_incident(state: dict[str, KnownIncident], incident_signature: str) -> KnownIncident | None:
    """Pure. None means this exact signature has never been resolved
    before -- the caller must route it to CODE_FIX_REQUIRED, never guess
    a recovery for it."""
    return state.get(incident_signature)


def record_occurrence(state: dict[str, KnownIncident], incident_signature: str, at: str) -> dict[str, KnownIncident]:
    """Pure. Bumps last_seen/occurrence_count for an ALREADY-known
    signature -- returns a new dict, never mutates the input. Raises if
    the signature isn't already known (recording an occurrence of an
    unknown incident is a contradiction; use add_resolved_incident for a
    genuinely new one)."""
    existing = state.get(incident_signature)
    if existing is None:
        raise KeyError(f"cannot record an occurrence of an unknown incident_signature: {incident_signature!r}")
    updated = replace(existing, last_seen=at, occurrence_count=existing.occurrence_count + 1)
    return {**state, incident_signature: updated}


def add_resolved_incident(state: dict[str, KnownIncident], incident: KnownIncident) -> dict[str, KnownIncident]:
    """Pure. Adds a brand-new KB entry -- used only after a genuinely new
    incident has been diagnosed and its recovery validated (by a human,
    per item 9: this layer never invents its own recovery for an unknown
    problem)."""
    return {**state, incident.incident_signature: incident}
