"""CONTINUOUS CAPITAL MANAGEMENT (user directive, 2026-08-25) --
prevents a repeat of the 2026-08-24 continuous-live incident: 10
consecutive Binance-buy/Bybit-sell RVN cycles drained Binance's USDT
from ~72.79 to 2.66 before a real BUY order was finally rejected
(insufficient balance). Nothing in the system previously asked "would
this trade leave the buy exchange dangerously short of USDT" before
submitting it -- every check that existed (classify_candidate,
compute_common_dual_leg_qty) only ever looked at the SELL exchange's
base-asset inventory, never the BUY exchange's own quote-asset reserve.

Pure, no I/O, no order placement anywhere in this module -- every
function here only classifies/decides using data the caller already
fetched fresh (real balances, real regime data). The actual REBALANCE
actions this module can recommend (SELL_TO_USDT, PREFER_OPPOSITE_
DIRECTION) still only ever resolve to calls into the existing, already-
tested constitute_inventory / execute_one_arbitrage -- this module adds
no new order-placement code path, only a decision layer in front of the
ones that already exist. No withdrawal, no blockchain transfer, no
cross-exchange movement -- explicitly out of scope by construction
(there is no function here that could express one).
"""

from dataclasses import dataclass
from enum import StrEnum


def compute_reserve_floor(max_notional_per_leg_usdt: float, *, multiplier: float = 2.5, min_floor: float = 20.0, max_floor: float = 25.0) -> float:
    """Pure function. The floor is deliberately DERIVED from the live
    per-leg notional cap, not a bare hardcoded number -- so it stays
    correct if that cap is ever changed, rather than silently going
    stale. multiplier=2.5 means: after reserving the floor, the exchange
    can still fund at least two more full-sized trades (a normal buy
    plus one more for genuine headroom) before running dry. Clamped to
    [min_floor, max_floor] to stay within the user's own "autour de
    20-25 USDT" instruction regardless of what the cap is configured to
    -- at the deployed default (10 USDT/leg), 2.5x lands exactly at the
    top of that range (25.0), which is the figure this module actually
    uses today; the clamp only matters if the cap is later raised."""
    return max(min_floor, min(max_floor, round(multiplier * max_notional_per_leg_usdt, 2)))


def compute_capital_imbalance_score(binance_usdt: float, bybit_usdt: float) -> float:
    """Pure function. 0.0 = perfectly balanced, approaching 1.0 = all
    the free USDT sits on one exchange. |a-b|/(a+b) -- deliberately NOT
    weighted by each exchange's target capital share (settings.py's
    binance_target_capital_usdt=100 / bybit_target_capital_usdt=60):
    the reserve floor this module enforces is symmetric, so the
    imbalance signal it feeds should be too. Returns 0.0 (not an error)
    when both sides are at/near zero -- there is no imbalance to speak
    of if there is no capital at all."""
    total = binance_usdt + bybit_usdt
    if total <= 0:
        return 0.0
    return abs(binance_usdt - bybit_usdt) / total


def is_rebalance_needed(binance_usdt: float, bybit_usdt: float, binance_floor: float, bybit_floor: float) -> bool:
    """Pure function. True the instant EITHER exchange is already below
    its own floor -- this is a present-tense fact check, not a
    prediction; compare with evaluate_reserve_impact for "would a
    specific trade push it below.\""""
    return binance_usdt < binance_floor or bybit_usdt < bybit_floor


@dataclass(slots=True)
class ReserveImpact:
    would_breach: bool
    post_trade_usdt: float
    shortfall_usdt: float  # 0.0 if would_breach is False; how far below the floor the trade would leave the exchange otherwise


def evaluate_reserve_impact(current_usdt: float, reserve_floor: float, trade_notional_usdt: float) -> ReserveImpact:
    """Pure function. The check this module exists to add: BEFORE
    submitting a buy leg, ask whether spending trade_notional_usdt on
    the buy exchange would leave it under its own floor -- never
    checked anywhere in this codebase before 2026-08-25."""
    post_trade = current_usdt - trade_notional_usdt
    breach = post_trade < reserve_floor
    return ReserveImpact(would_breach=breach, post_trade_usdt=post_trade, shortfall_usdt=max(0.0, reserve_floor - post_trade))


class TradeDecision(StrEnum):
    ALLOW = "ALLOW"
    DO_NOT_TRADE = "DO_NOT_TRADE"
    REBALANCE_FIRST = "REBALANCE_FIRST"
    PREFER_OPPOSITE_DIRECTION = "PREFER_OPPOSITE_DIRECTION"


@dataclass(slots=True)
class TradeDecisionResult:
    decision: TradeDecision
    reason: str
    impact: ReserveImpact


def decide_trade_with_reserve_check(
    *,
    buy_exchange_usdt: float,
    buy_exchange_floor: float,
    trade_notional_usdt: float,
    opposite_direction_available_and_profitable: bool = False,
    reconvertible_inventory_value_usdt_on_buy_exchange: float = 0.0,
) -> TradeDecisionResult:
    """Pure function -- the core rule the user asked for: "Si un trade
    ferait passer l'exchange acheteur sous son reserve floor : DO NOT
    TRADE ou rebalance d'abord intelligemment." Priority order (this
    module's own, disclosed choice -- the directive lists three
    mechanisms without ranking them): a genuinely profitable OPPOSITE-
    direction trade is preferred first (it fixes the imbalance AS a
    normal profitable trade, no unwind cost); failing that, reconverting
    already-held inventory back to USDT is the next lever (it costs a
    taker fee/spread but stays useful); DO_NOT_TRADE is the last resort
    when neither is available -- never breach the floor to force a
    trade through."""
    impact = evaluate_reserve_impact(buy_exchange_usdt, buy_exchange_floor, trade_notional_usdt)
    if not impact.would_breach:
        return TradeDecisionResult(TradeDecision.ALLOW, "trade does not breach the buy exchange's reserve floor", impact)
    if opposite_direction_available_and_profitable:
        return TradeDecisionResult(
            TradeDecision.PREFER_OPPOSITE_DIRECTION,
            f"buying {trade_notional_usdt} USDT worth here would leave only {impact.post_trade_usdt:.2f} "
            f"(floor {buy_exchange_floor}) -- a genuinely profitable opposite-direction candidate exists and would "
            "REPLENISH this exchange's USDT instead of depleting it further",
            impact,
        )
    if reconvertible_inventory_value_usdt_on_buy_exchange >= impact.shortfall_usdt > 0:
        return TradeDecisionResult(
            TradeDecision.REBALANCE_FIRST,
            f"would breach the floor by {impact.shortfall_usdt:.2f} USDT, but {reconvertible_inventory_value_usdt_on_buy_exchange:.2f} "
            "USDT of existing inventory on this same exchange can be sold back to USDT first to restore headroom",
            impact,
        )
    return TradeDecisionResult(
        TradeDecision.DO_NOT_TRADE,
        f"would breach the floor by {impact.shortfall_usdt:.2f} USDT and neither a profitable opposite-direction "
        "candidate nor enough reconvertible inventory is available -- skip rather than breach the reserve",
        impact,
    )


class InventoryAction(StrEnum):
    KEEP = "KEEP"
    REUSE = "REUSE"
    SELL_TO_USDT = "SELL_TO_USDT"
    DUST = "DUST"


@dataclass(slots=True)
class InventoryDecision:
    action: InventoryAction
    reason: str


def classify_inventory_position(
    *,
    value_usdt: float,
    min_notional: float,
    currently_qualifying: bool,
    exchange_below_floor: bool,
    is_top_reconversion_candidate_on_this_exchange: bool,
) -> InventoryDecision:
    """Pure function. Priority order: DUST is a hard floor (nothing
    useful can be done with an amount that can't even clear the
    exchange's own MIN_NOTIONAL alone) -- checked first regardless of
    any other signal. Then: if THIS exchange is short of its USDT
    reserve and this position is the single largest reconvertible
    holding on it, that is the natural, lowest-friction lever
    (SELL_TO_USDT) -- selling the biggest position moves the most USDT
    per trade, minimizing how many extra taker fees a rebalance costs.
    Otherwise, a position in a symbol that is CURRENTLY showing a
    qualifying real edge is worth REUSING (kept specifically because it
    is likely to be sold into a real arbitrage again soon) rather than
    generically KEPT."""
    if value_usdt < min_notional:
        return InventoryDecision(InventoryAction.DUST, f"value {value_usdt:.4f} USDT below this exchange's own min_notional {min_notional} -- too small to usefully trade or convert alone")
    if exchange_below_floor and is_top_reconversion_candidate_on_this_exchange:
        return InventoryDecision(InventoryAction.SELL_TO_USDT, "this exchange's USDT reserve is below its floor and this is the largest reconvertible position on it -- the lowest-friction rebalancing lever")
    if currently_qualifying:
        return InventoryDecision(InventoryAction.REUSE, "symbol currently shows a qualifying real short-term edge (CONFIRMED_SHORT_TERM or better) -- likely to be sold into a real arbitrage again soon")
    return InventoryDecision(InventoryAction.KEEP, "no immediate reserve pressure on this exchange and no currently-qualifying edge -- no action needed right now")


def compute_rebalance_realized_pnl(qty_sold: float, sell_price_usdt: float, cost_basis_usdt_per_unit: float | None, fee_usd: float) -> float | None:
    """Pure function (user directive, 2026-08-25, REBALANCING_PNL). The
    REALIZED gain/loss from a rebalance sell -- selling inventory back to
    USDT is a real trade with a real result, not a free/neutral action:
    "un arbitrage a +0.20 USDT suivi d'un rebalance coutant -0.05 USDT
    doit contribuer seulement +0.15 USDT au resultat economique global."
    Returns None (never a fabricated number) when the cost basis is
    unknown -- matches app.reporting.live_trading_dashboard's own
    weighted-average cost-basis convention, so a rebalance's P&L and the
    dashboard's inventory unrealized-P&L are always computed the same way."""
    if cost_basis_usdt_per_unit is None:
        return None
    return (sell_price_usdt - cost_basis_usdt_per_unit) * qty_sold - fee_usd


@dataclass(slots=True)
class ReplayEvent:
    """One real historical USDT-spending event -- either an arbitrage
    buy leg or an inventory constitution, whichever ledger it came from.
    kind is informational only; spend_exchange/spend_notional is what
    the reserve check actually acts on regardless of kind, which is the
    whole point (item 1 of the 2026-08-25 integration directive: the
    same check applies before arbitrage BUY, inventory constitution, AND
    inventory recycling)."""

    at: object  # datetime -- left untyped to avoid importing datetime just for a replay-only sort key
    kind: str
    symbol: str
    spend_exchange: str
    receive_exchange: str | None
    spend_notional_usdt: float
    receive_notional_usdt: float
    price_usdt: float
    qty_received_net: float
    qty_sold: float


@dataclass(slots=True)
class ReplayStepResult:
    event: ReplayEvent
    decision: TradeDecisionResult
    binance_usdt_after: float
    bybit_usdt_after: float


@dataclass(slots=True)
class ReplayResult:
    steps: list[ReplayStepResult]
    min_binance_usdt: float
    min_bybit_usdt: float
    end_binance_usdt: float
    end_bybit_usdt: float
    interventions: int


def simulate_event_sequence(
    events: list[ReplayEvent],
    *,
    starting_binance_usdt: float,
    starting_bybit_usdt: float,
    binance_floor: float,
    bybit_floor: float,
    taker_fee_rate: float = 0.001,
) -> ReplayResult:
    """Pure function, no I/O. Replays a chronological sequence of real
    USDT-spending events through decide_trade_with_reserve_check,
    simulating the SAME REBALANCE_FIRST mechanics the live orchestrator
    uses: sell just enough already-accumulated same-exchange inventory
    (never more than the real shortfall, per the user's own "utiliser
    uniquement la quantite minimale necessaire") to clear the floor,
    then let the event proceed. DO_NOT_TRADE skips the event entirely.
    opposite-direction preference is NOT modeled here (it would require
    re-deriving what alternative candidates were live-scanned at each
    historical instant, which isn't reconstructable after the fact) --
    this makes simulate_event_sequence a deliberate LOWER BOUND on how
    much the real integrated system would help, not an exact replay.

    This is the same logic tests/test_capital_rebalancer_replay.py runs
    against the 31 real historical events from 2026-08-24 as a
    permanent regression -- proving this specific incident (Binance
    draining to 2.66 USDT) cannot silently recur uncaught."""
    usdt = {"binance": starting_binance_usdt, "bybit": starting_bybit_usdt}
    base: dict[str, dict[str, float]] = {"binance": {}, "bybit": {}}
    steps: list[ReplayStepResult] = []
    min_binance = starting_binance_usdt
    min_bybit = starting_bybit_usdt
    interventions = 0

    for event in events:
        base_asset = event.symbol.removesuffix("USDT")
        spend_exch = event.spend_exchange
        floor = binance_floor if spend_exch == "binance" else bybit_floor
        reconvertible_value = base[spend_exch].get(base_asset, 0.0) * event.price_usdt

        decision = decide_trade_with_reserve_check(
            buy_exchange_usdt=usdt[spend_exch], buy_exchange_floor=floor, trade_notional_usdt=event.spend_notional_usdt,
            opposite_direction_available_and_profitable=False,
            reconvertible_inventory_value_usdt_on_buy_exchange=reconvertible_value,
        )
        if decision.decision != TradeDecision.ALLOW:
            interventions += 1

        if decision.decision == TradeDecision.DO_NOT_TRADE:
            steps.append(ReplayStepResult(event, decision, usdt["binance"], usdt["bybit"]))
            min_binance, min_bybit = min(min_binance, usdt["binance"]), min(min_bybit, usdt["bybit"])
            continue

        if decision.decision == TradeDecision.REBALANCE_FIRST:
            qty_to_sell = min(decision.impact.shortfall_usdt / event.price_usdt if event.price_usdt else 0.0, base[spend_exch].get(base_asset, 0.0))
            proceeds = qty_to_sell * event.price_usdt * (1 - taker_fee_rate)
            base[spend_exch][base_asset] = base[spend_exch].get(base_asset, 0.0) - qty_to_sell
            usdt[spend_exch] += proceeds

        usdt[spend_exch] -= event.spend_notional_usdt
        base[spend_exch][base_asset] = base[spend_exch].get(base_asset, 0.0) + event.qty_received_net
        if event.receive_exchange:
            base[event.receive_exchange][base_asset] = base[event.receive_exchange].get(base_asset, 0.0) - event.qty_sold
            usdt[event.receive_exchange] += event.receive_notional_usdt

        steps.append(ReplayStepResult(event, decision, usdt["binance"], usdt["bybit"]))
        min_binance, min_bybit = min(min_binance, usdt["binance"]), min(min_bybit, usdt["bybit"])

    return ReplayResult(steps=steps, min_binance_usdt=min_binance, min_bybit_usdt=min_bybit, end_binance_usdt=usdt["binance"], end_bybit_usdt=usdt["bybit"], interventions=interventions)
