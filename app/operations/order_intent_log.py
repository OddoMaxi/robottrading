"""ORDER INTENT LOG (user directive, 2026-08-25, AUTONOMOUS 24/7
persistence, item 13 -- "CHECK UNKNOWN ORDERS" before SAFE_TO_RESUME). A
durable, JSON-backed record of "a real order-capable operation was
started," written BEFORE the operation begins and marked resolved AFTER
it returns -- regardless of outcome, including a rejection or no-fill,
since those are legitimate, clean resolutions. The point is not to
record what happened, only whether the operation reached a conclusion at
all: a crash mid-operation leaves an UNRESOLVED intent a restart can
find, even though the in-memory client_order_id itself is lost with the
crashed process.

Coarser than a per-order ledger for the two calls this orchestrator does
not control internally (execute_one_arbitrage, constitute_inventory --
their own client_order_id generation is private to those modules, which
this layer is not permitted to modify per the user's own "ne modifie pas
les executors"): the intent records the CALL itself (purpose, exchange,
symbol, notional, started_at), not the specific order id(s) it may
submit internally. For the one call this orchestrator DOES fully control
(a rebalance sell, reimplemented directly in the orchestrator script),
the intent can carry the exact client_order_id for precise follow-up.

An unresolved intent found at startup is always treated conservatively:
this module does not attempt to guess what happened (no fills-search, no
inference) -- it only reports the fact. The caller (the orchestrator's
own pre-flight sequence) treats ANY unresolved intent as HUMAN_REVIEW_
REQUIRED, never auto-resolved, matching this project's standing rule to
never invent an explanation for ambiguous real-money state.

Pure core (start_intent/resolve_intent/unresolved_intents operate on a
plain dict, no I/O, no clock/uuid reads -- both `at` and `intent_id` are
always caller-supplied); load_state/save_state are the only I/O,
isolated at the edges, matching every other app.operations module."""

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

DEFAULT_STATE_PATH = Path("/opt/robotcripto/data/order_intent_log.json")


@dataclass(slots=True, frozen=True)
class OrderIntent:
    intent_id: str
    purpose: str  # "ARBITRAGE" | "INVENTORY_CONSTITUTION" | "REBALANCE_SELL"
    exchange: str
    symbol: str
    notional_usdt: float | None
    client_order_id: str | None  # precisely known only for REBALANCE_SELL
    started_at: str
    resolved: bool
    resolved_at: str | None
    resolved_outcome: str | None


IntentLogState = dict[str, OrderIntent]


def start_intent(
    state: IntentLogState, *, intent_id: str, purpose: str, exchange: str, symbol: str, at: str,
    notional_usdt: float | None = None, client_order_id: str | None = None,
) -> IntentLogState:
    """Pure. Call BEFORE the real operation begins. Returns a new state,
    never mutates the input."""
    intent = OrderIntent(
        intent_id=intent_id, purpose=purpose, exchange=exchange, symbol=symbol, notional_usdt=notional_usdt,
        client_order_id=client_order_id, started_at=at, resolved=False, resolved_at=None, resolved_outcome=None,
    )
    return {**state, intent_id: intent}


def resolve_intent(state: IntentLogState, intent_id: str, *, at: str, outcome: str) -> IntentLogState:
    """Pure. Call AFTER the real operation returns, REGARDLESS of
    outcome -- a clean rejection or no-fill is still a resolution.
    Raises if the intent isn't known (resolving something never started
    is a contradiction)."""
    existing = state.get(intent_id)
    if existing is None:
        raise KeyError(f"cannot resolve an intent that was never started: {intent_id!r}")
    updated = replace(existing, resolved=True, resolved_at=at, resolved_outcome=outcome)
    return {**state, intent_id: updated}


def unresolved_intents(state: IntentLogState) -> list[OrderIntent]:
    """Pure. Every intent still waiting for its resolution -- non-empty
    at startup means something was in flight when the process last
    stopped, for a reason this module does not try to determine."""
    return [i for i in state.values() if not i.resolved]


def load_state(path: Path = DEFAULT_STATE_PATH) -> IntentLogState:
    if not path.exists():
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {intent_id: OrderIntent(**entry) for intent_id, entry in raw.items()}


def save_state(state: IntentLogState, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({intent_id: asdict(i) for intent_id, i in state.items()}, f, indent=2, sort_keys=True)
