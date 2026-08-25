"""BALANCE RECONCILIATION (user directive, 2026-08-24, FIX 2; extended
2026-08-25, FIX 3). Verifies a real, observed base-asset balance delta
against everything a batch attempt could actually have done to it: an
optional inventory constitution fill, an arbitrage buy fill, an
arbitrage sell fill, an optional neutralization (flatten) fill, and an
optional capital-rebalancing sell -- each only contributing when it
happened ON THE EXCHANGE being checked.

The one-off batch script's original reconcile check (2026-08-24) compared
the balance after a cycle to the balance captured BEFORE that same
cycle's own inventory constitution step -- so a real SAND cycle that
correctly constituted 236.763 net SAND on Binance then sold 232.0 of it
there (leaving 4.763, exactly as observed) was flagged as a 232 SAND
"deficit", purely because the constitution step's own contribution was
never added into the expected side of the equation. FIX 3 (2026-08-25)
closes an identical gap introduced by the capital rebalancer: a real RVN
cycle sold 2192.5 RVN via REBALANCE_FIRST on Binance, then bought back
net 2125.1727 RVN there via the arbitrage leg -- this module only knew
about the buy, so it flagged a false -2192.5 "mismatch" that was, in
fact, fully explained by the rebalance sell (verified independently
against real Binance myTrades: orders 1344692249 and 1344692270). This
module fixes both with one explicit identity:

    expected_after = before
                    + inventory_constitution_net_qty (if constituted here)
                    + arbitrage_buy_net_qty            (if bought here)
                    - arbitrage_sell_qty                (if sold here)
                    - neutralization_qty                (if flattened here)
                    - rebalance_sell_qty                (if rebalance-sold here)

Pure function, no I/O, no order placement -- reads results that
app.execution.inventory_constitution_executor / live_arbitrage_executor /
app.execution.capital_rebalancer's own callers already produced and
persisted."""

from dataclasses import dataclass


@dataclass(slots=True)
class ReconciliationResult:
    expected_delta: float
    actual_delta: float
    difference: float
    tolerance: float
    match: bool
    explanation: str


def reconcile_base_asset_balance(
    *,
    exchange: str,
    before_balance: float,
    after_balance: float,
    inventory_constitution_exchange: str | None = None,
    inventory_constitution_net_qty: float = 0.0,
    arbitrage_buy_exchange: str | None = None,
    arbitrage_buy_net_qty: float = 0.0,
    arbitrage_sell_exchange: str | None = None,
    arbitrage_sell_qty: float = 0.0,
    neutralization_exchange: str | None = None,
    neutralization_qty: float = 0.0,
    rebalance_sell_exchange: str | None = None,
    rebalance_sell_qty: float = 0.0,
    tolerance_abs: float = 0.05,
    tolerance_rel: float = 0.02,
) -> ReconciliationResult:
    """Pure function. `exchange` is the one being checked ("binance" or
    "bybit"); every other `*_exchange` argument says WHERE that specific
    leg actually happened -- its quantity only contributes to
    expected_delta when it matches `exchange`, so calling this once per
    exchange with the SAME attempt's full data naturally reconciles both
    sides. Neutralization and rebalance-selling are always sells (they
    flatten an unwanted position / raise USDT headroom), so they always
    subtract, exactly like an arbitrage sell. A single cycle can only
    ever rebalance-sell on ONE of the two exchanges per USDT-spending
    action it gates (buy_exchange and sell_exchange always differ in a
    cross-exchange arbitrage) -- a scalar is sufficient, matching every
    other leg here.

    tolerance is the larger of tolerance_abs and tolerance_rel *
    |expected_delta| -- a small, disclosed allowance for dust/rounding,
    never a silent excuse for an unexplained gap: it must be documented
    at the call site, not tuned away a mismatch."""
    expected_delta = 0.0
    parts: list[str] = []
    if inventory_constitution_exchange == exchange and inventory_constitution_net_qty:
        expected_delta += inventory_constitution_net_qty
        parts.append(f"+{inventory_constitution_net_qty} (inventory constitution net fill)")
    if arbitrage_buy_exchange == exchange and arbitrage_buy_net_qty:
        expected_delta += arbitrage_buy_net_qty
        parts.append(f"+{arbitrage_buy_net_qty} (arbitrage buy net fill)")
    if arbitrage_sell_exchange == exchange and arbitrage_sell_qty:
        expected_delta -= arbitrage_sell_qty
        parts.append(f"-{arbitrage_sell_qty} (arbitrage sell fill)")
    if neutralization_exchange == exchange and neutralization_qty:
        expected_delta -= neutralization_qty
        parts.append(f"-{neutralization_qty} (neutralization sell fill)")
    if rebalance_sell_exchange == exchange and rebalance_sell_qty:
        expected_delta -= rebalance_sell_qty
        parts.append(f"-{rebalance_sell_qty} (capital-rebalancing sell fill)")

    actual_delta = after_balance - before_balance
    difference = actual_delta - expected_delta
    tolerance = max(tolerance_abs, abs(expected_delta) * tolerance_rel)
    match = abs(difference) <= tolerance

    explanation = (
        f"exchange={exchange}: expected_delta={expected_delta} ({', '.join(parts) if parts else 'no fills on this exchange'}), "
        f"actual_delta={actual_delta} (before={before_balance}, after={after_balance}), "
        f"difference={difference}, tolerance={tolerance} -> {'MATCH' if match else 'MISMATCH'}"
    )
    return ReconciliationResult(
        expected_delta=expected_delta, actual_delta=actual_delta, difference=difference,
        tolerance=tolerance, match=match, explanation=explanation,
    )
