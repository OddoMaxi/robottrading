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
    PROCEED = "PROCEED"
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
        return TradeDecisionResult(TradeDecision.PROCEED, "trade does not breach the buy exchange's reserve floor", impact)
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
