"""V4 SESSION REPLAY (user directive, 2026-08-25, V5 item 6 -- "REPLAY
THE ENTIRE V4 SESSION"). Two independent, pure replays over the same
real fill data:

1. replay_wealth_bridge -- chronologically applies every real fill to
   app.execution.true_economic_ledger and totals realized + unrealized
   PNL. This must reproduce the forensic reconstruction's real wealth
   change (-7.135227 USD, verified against real balances/prices
   independently of any trade data) to within a cent -- it is the same
   mechanism, now productionized and tested, not a new claim.

2. replay_v4_decisions_through_v5_gate -- the counterfactual: replays
   every real V4 arbitrage cycle chronologically, but at each cycle's
   decision point asks app.execution.true_economic_pretrade whether V5
   would have taken it, using the REAL pool state as it stood at that
   exact point in V5's OWN alternate history (never V4's), and only
   commits the cycle's cash/pool effects when accepted. Rebalances and
   inventory constitutions are not independently gated (a full
   counterfactual re-simulation of the live scanning/inventory logic is
   out of scope) -- they are checked for continued NECESSITY against the
   simulated capital/pool state using the same real production functions
   V4 itself uses (app.execution.capital_rebalancer.evaluate_reserve_
   impact, compute_reserve_floor), and applied only when still necessary.

Known, disclosed simplification: buy_side_mark_price (the independent
valuation used to price a fresh buy for EXPECTED_BUY_INVENTORY_DELTA) is
not available historically -- no independent bid snapshot was archived
at each real decision point. This replay uses the real buy fill's own
price as a stand-in, which is more favorable to accepting a trade than a
true conservative bid mark would be (crossing the spread always costs
something in reality). The counterfactual ACCEPT count below is
therefore a ceiling on what V5 would accept live, not a floor."""

from dataclasses import dataclass

from app.execution.capital_rebalancer import compute_reserve_floor, evaluate_reserve_impact
from app.execution.true_economic_ledger import LedgerState, apply_buy, apply_sell, get_pool, put_pool, seed_pool
from app.execution.true_economic_pretrade import evaluate_arbitrage_true_economics

QUOTE_ASSET = "USDT"


@dataclass(slots=True, frozen=True)
class ArbitrageCycleEvent:
    attempt_id: str
    ts_ms: int
    symbol: str
    base_asset: str
    buy_exchange: str
    sell_exchange: str
    buy_qty: float
    buy_price: float
    buy_fee_amount: float
    buy_fee_asset: str
    sell_qty: float
    sell_price: float
    sell_fee_amount: float
    sell_fee_asset: str


@dataclass(slots=True, frozen=True)
class InventoryBuyEvent:
    ts_ms: int
    exchange: str
    asset: str
    qty: float
    price: float
    fee_amount: float
    fee_asset: str


@dataclass(slots=True, frozen=True)
class RebalanceSellEvent:
    ts_ms: int
    exchange: str
    asset: str
    qty: float
    price: float
    fee_amount: float
    fee_asset: str


ReplayEvent = ArbitrageCycleEvent | InventoryBuyEvent | RebalanceSellEvent


def build_events_from_fills(fills: list[dict]) -> list[ReplayEvent]:
    """Pure. Groups raw fills (the same dict shape produced by pulling
    real Binance myTrades / Bybit execution-list data: exchange, symbol,
    base_asset, side, qty, quote_qty, price, commission, commission_asset,
    ts_ms, client_order_id/order_id, purpose) into typed events, sorted
    chronologically. Fills belonging to the SAME real order (order_id can
    partial-fill more than once, especially on Bybit) are aggregated to
    their notional-weighted average price before becoming one event --
    for ARBITRAGE_BUY/ARBITRAGE_SELL this then pairs by the attempt_id
    embedded in the client_order_id (buy-{id}/sell-{id}); INVENTORY_BUY
    and REBALANCE_SELL are aggregated the same way so "how many
    rebalances/inventory actions happened" matches V4's own per-order
    event counters, not an inflated per-fill count."""
    buy_fills: dict[str, list[dict]] = {}
    sell_fills: dict[str, list[dict]] = {}
    inventory_orders: dict[tuple[str, str], list[dict]] = {}
    rebalance_orders: dict[tuple[str, str], list[dict]] = {}
    events: list[ReplayEvent] = []

    for f in fills:
        coid = f.get("client_order_id") or ""
        if coid.startswith("buy-"):
            buy_fills.setdefault(coid[len("buy-"):], []).append(f)
        elif coid.startswith("sell-"):
            sell_fills.setdefault(coid[len("sell-"):], []).append(f)
        elif f["purpose"] == "INVENTORY_BUY":
            inventory_orders.setdefault((f["exchange"], f["order_id"]), []).append(f)
        elif f["purpose"] == "REBALANCE_SELL":
            rebalance_orders.setdefault((f["exchange"], f["order_id"]), []).append(f)

    def _aggregate(order_fills: list[dict]) -> tuple[float, float, float]:
        qty = sum(x["qty"] for x in order_fills)
        notional = sum(x["quote_qty"] for x in order_fills)
        fee = sum(x["commission"] for x in order_fills)
        return qty, (notional / qty if qty else 0.0), fee

    for order_fills in inventory_orders.values():
        qty, price, fee = _aggregate(order_fills)
        events.append(InventoryBuyEvent(
            ts_ms=min(x["ts_ms"] for x in order_fills), exchange=order_fills[0]["exchange"], asset=order_fills[0]["base_asset"],
            qty=qty, price=price, fee_amount=fee, fee_asset=order_fills[0]["commission_asset"],
        ))
    for order_fills in rebalance_orders.values():
        qty, price, fee = _aggregate(order_fills)
        events.append(RebalanceSellEvent(
            ts_ms=min(x["ts_ms"] for x in order_fills), exchange=order_fills[0]["exchange"], asset=order_fills[0]["base_asset"],
            qty=qty, price=price, fee_amount=fee, fee_asset=order_fills[0]["commission_asset"],
        ))

    for attempt_id in sorted(set(buy_fills) & set(sell_fills)):
        bfills, sfills = buy_fills[attempt_id], sell_fills[attempt_id]
        buy_qty, buy_price, buy_fee = _aggregate(bfills)  # single fee currency per real order
        sell_qty, sell_price, sell_fee = _aggregate(sfills)
        events.append(ArbitrageCycleEvent(
            attempt_id=attempt_id, ts_ms=min(x["ts_ms"] for x in bfills + sfills), symbol=bfills[0]["symbol"],
            base_asset=bfills[0]["base_asset"], buy_exchange=bfills[0]["exchange"], sell_exchange=sfills[0]["exchange"],
            buy_qty=buy_qty, buy_price=buy_price, buy_fee_amount=buy_fee, buy_fee_asset=bfills[0]["commission_asset"],
            sell_qty=sell_qty, sell_price=sell_price, sell_fee_amount=sell_fee, sell_fee_asset=sfills[0]["commission_asset"],
        ))

    events.sort(key=lambda e: e.ts_ms)
    return events


@dataclass(slots=True, frozen=True)
class WealthBridgeResult:
    total_realized_pnl_usd: float
    total_unrealized_pnl_usd: float
    total_wealth_change_usd: float
    ending_pools: LedgerState


def replay_wealth_bridge(
    fills: list[dict], starting_pools: dict[tuple[str, str], tuple[float, float]],
    current_prices: dict[tuple[str, str], float],
) -> WealthBridgeResult:
    """Pure. starting_pools/current_prices keyed by (exchange, asset).
    Chronologically applies every real fill (regardless of purpose --
    every BUY/SELL, of any kind, updates the same shared pool, exactly
    matching how fungible coins actually work) and totals realized +
    unrealized PNL. Assets present in starting_pools but never touched by
    any fill (e.g. MANTRA this session) still contribute their pure
    price-effect via the unrealized term."""
    state: LedgerState = {}
    for (exchange, asset), (qty, price) in starting_pools.items():
        state = seed_pool(state, exchange, asset, qty=qty, price=price)

    total_realized = 0.0
    for f in sorted(fills, key=lambda f: f["ts_ms"]):
        pool = get_pool(state, f["exchange"], f["base_asset"])
        if f["side"] == "BUY":
            state = put_pool(state, apply_buy(pool, qty=f["qty"], price=f["price"], fee_amount=f["commission"], fee_asset=f["commission_asset"]))
        else:
            result = apply_sell(pool, qty=f["qty"], price=f["price"], fee_amount=f["commission"], fee_asset=f["commission_asset"])
            if result is None:
                continue  # data gap; surfaced separately by callers that check pool integrity
            state = put_pool(state, result.pool)
            total_realized += result.realized_pnl_usd

    total_unrealized = 0.0
    for (exchange, asset), price in current_prices.items():
        pool = get_pool(state, exchange, asset)
        total_unrealized += pool.qty * price - pool.cost_usd

    return WealthBridgeResult(
        total_realized_pnl_usd=total_realized, total_unrealized_pnl_usd=total_unrealized,
        total_wealth_change_usd=total_realized + total_unrealized, ending_pools=state,
    )


@dataclass(slots=True, frozen=True)
class CycleDecision:
    attempt_id: str
    ts_ms: int
    symbol: str
    accepted: bool
    true_wealth_delta_usd: float | None
    reason: str


@dataclass(slots=True, frozen=True)
class V4ReplayReport:
    decisions: list[CycleDecision]
    accepted_count: int
    rejected_count: int
    accepted_true_pnl_usd: float
    rejected_true_pnl_usd: float
    rebalances_seen: int
    rebalances_avoided: int
    inventory_actions_seen: int
    inventory_actions_avoided: int
    ending_pools: LedgerState


def replay_v4_decisions_through_v5_gate(
    events: list[ReplayEvent], starting_pools: dict[tuple[str, str], tuple[float, float]],
    starting_usdt: dict[str, float], *, max_notional_per_leg_usdt: float = 10.0, required_safety_margin_usd: float = 0.0,
) -> V4ReplayReport:
    """Pure. The counterfactual walk described in this module's docstring.
    max_notional_per_leg_usdt feeds compute_reserve_floor exactly as the
    real deployed V4 orchestrator does (25.0 USDT at the deployed 10
    USDT/leg default) -- this replay reuses that real, tested production
    function rather than inventing a separate threshold."""
    state: LedgerState = {}
    for (exchange, asset), (qty, price) in starting_pools.items():
        state = seed_pool(state, exchange, asset, qty=qty, price=price)
    usdt = dict(starting_usdt)
    reserve_floor = compute_reserve_floor(max_notional_per_leg_usdt)

    decisions: list[CycleDecision] = []
    accepted_true_pnl = 0.0
    rejected_true_pnl = 0.0
    rebalances_seen = rebalances_avoided = 0
    inventory_seen = inventory_avoided = 0

    for event in events:
        if isinstance(event, ArbitrageCycleEvent):
            sell_pool = get_pool(state, event.sell_exchange, event.base_asset)
            buy_pool = get_pool(state, event.buy_exchange, event.base_asset)
            quote = evaluate_arbitrage_true_economics(
                sell_pool=sell_pool, sell_qty=event.sell_qty, sell_price=event.sell_price,
                sell_fee_amount=event.sell_fee_amount, sell_fee_asset=event.sell_fee_asset,
                buy_pool=buy_pool, buy_qty=event.buy_qty, buy_price=event.buy_price,
                buy_fee_amount=event.buy_fee_amount, buy_fee_asset=event.buy_fee_asset,
                buy_side_mark_price=event.buy_price,  # disclosed simplification, see module docstring
                required_safety_margin_usd=required_safety_margin_usd,
            )
            if quote.would_trade and quote.new_sell_pool is not None and quote.new_buy_pool is not None:
                state = put_pool(state, quote.new_sell_pool)
                state = put_pool(state, quote.new_buy_pool)
                usdt[event.buy_exchange] = usdt.get(event.buy_exchange, 0.0) - quote.new_buy_cost_usd
                usdt[event.sell_exchange] = usdt.get(event.sell_exchange, 0.0) + (quote.expected_net_sell_proceeds_usd or 0.0)
                accepted_true_pnl += quote.expected_true_wealth_delta_usd or 0.0
            else:
                rejected_true_pnl += quote.expected_true_wealth_delta_usd or 0.0
            decisions.append(CycleDecision(
                attempt_id=event.attempt_id, ts_ms=event.ts_ms, symbol=event.symbol, accepted=quote.would_trade,
                true_wealth_delta_usd=quote.expected_true_wealth_delta_usd, reason=quote.reason,
            ))

        elif isinstance(event, InventoryBuyEvent):
            inventory_seen += 1
            pool = get_pool(state, event.exchange, event.asset)
            already_ample = pool.qty >= event.qty  # V5's simulated pool already holds at least what this buy would add
            if already_ample:
                inventory_avoided += 1
            else:
                state = put_pool(state, apply_buy(pool, qty=event.qty, price=event.price, fee_amount=event.fee_amount, fee_asset=event.fee_asset))
                usdt[event.exchange] = usdt.get(event.exchange, 0.0) - (event.qty * event.price + (event.fee_amount if event.fee_asset == QUOTE_ASSET else 0.0))

        elif isinstance(event, RebalanceSellEvent):
            rebalances_seen += 1
            # trade_notional_usdt=0.0: this asks the real reserve-floor
            # check a simpler question than V4's own call site does --
            # "is the simulated balance already at/above the floor with
            # no further spend," i.e. would this rebalance still be
            # needed for no other reason than being under the floor
            # right now.
            impact = evaluate_reserve_impact(usdt.get(event.exchange, 0.0), reserve_floor, 0.0)
            if not impact.would_breach:
                rebalances_avoided += 1
            else:
                pool = get_pool(state, event.exchange, event.asset)
                result = apply_sell(pool, qty=event.qty, price=event.price, fee_amount=event.fee_amount, fee_asset=event.fee_asset)
                if result is not None:
                    state = put_pool(state, result.pool)
                    usdt[event.exchange] = usdt.get(event.exchange, 0.0) + result.net_proceeds_usd

    accepted = [d for d in decisions if d.accepted]
    rejected = [d for d in decisions if not d.accepted]
    return V4ReplayReport(
        decisions=decisions, accepted_count=len(accepted), rejected_count=len(rejected),
        accepted_true_pnl_usd=accepted_true_pnl, rejected_true_pnl_usd=rejected_true_pnl,
        rebalances_seen=rebalances_seen, rebalances_avoided=rebalances_avoided,
        inventory_actions_seen=inventory_seen, inventory_actions_avoided=inventory_avoided,
        ending_pools=state,
    )
