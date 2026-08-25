"""TRUE ECONOMIC PRE-TRADE DECISION ENGINE (user directive, 2026-08-25,
V5). Replaces the V4 decision rule (app.execution.live_arbitrage_executor
line 747: `actual_net_pnl_usd = sell_proceeds_usd - buy_cost_usd`, i.e.
"this cycle's sell proceeds minus this cycle's buy cost") with a rule
that asks the only question that actually matters: does the COMPLETE
operation increase real net worth, once the asset actually being sold is
valued at ITS OWN true cost basis (app.execution.true_economic_ledger),
not at what it currently costs to buy an equivalent amount somewhere
else.

Every dollar amount below is a SIGNED contribution to wealth -- summed
directly, never subtracted a second time. Fees are already embedded in
sell_side_realized_pnl (reduces proceeds) and in new_buy_cost (increases
cost); EXPECTED_TOTAL_FEES is reported for transparency only, exactly as
the forensic reconstruction's TOTAL_REAL_FEES was -- it is a memo, not an
additional bridge term.

EXPECTED_TRUE_WEALTH_DELTA = SELL_SIDE_REALIZED_PNL
                            + EXPECTED_BUY_INVENTORY_DELTA
                            + REBALANCE_IMPACT_USD (0.0 if none needed)

A trade is only authorized when this exceeds REQUIRED_SAFETY_MARGIN_USD.
No other input (the size of OLD_EDGE, the predicted spread, anything
quote-based) enters the decision."""

from dataclasses import dataclass

from app.execution.true_economic_ledger import CostBasisPool, apply_buy, apply_sell

QUOTE_ASSET = "USDT"


@dataclass(slots=True, frozen=True)
class RebalanceSimulation:
    """The result of pre-simulating a rebalance sell BEFORE the arbitrage
    it would fund is allowed to proceed (item 3, user directive) -- reuses
    the exact same depleting cost-basis mechanic as every other sell in
    this system, replacing app.reporting.live_trading_dashboard.
    compute_cost_basis_by_asset_exchange (the non-depleting, lifetime-
    average function identified as root cause #2 of the V4 gap)."""

    new_pool: CostBasisPool
    cost_basis_of_units_sold_usd: float
    net_proceeds_usd: float
    realized_pnl_usd: float
    fee_usd_equivalent: float


def simulate_rebalance(
    pool: CostBasisPool, *, qty_to_sell: float, price: float, fee_amount: float, fee_asset: str,
    quote_asset: str = QUOTE_ASSET,
) -> RebalanceSimulation | None:
    """Pure. None (never fabricated) if the pool cannot cover the sale."""
    result = apply_sell(pool, qty=qty_to_sell, price=price, fee_amount=fee_amount, fee_asset=fee_asset, quote_asset=quote_asset)
    if result is None:
        return None
    fee_usd = fee_amount * price if fee_asset != quote_asset else fee_amount
    return RebalanceSimulation(
        new_pool=result.pool, cost_basis_of_units_sold_usd=result.cost_basis_of_units_sold_usd,
        net_proceeds_usd=result.net_proceeds_usd, realized_pnl_usd=result.realized_pnl_usd, fee_usd_equivalent=fee_usd,
    )


@dataclass(slots=True, frozen=True)
class TrueEconomicQuote:
    sell_inventory_cost_basis_usd: float | None
    expected_net_sell_proceeds_usd: float | None
    sell_side_realized_pnl_usd: float | None
    new_buy_cost_usd: float
    new_buy_mark_to_market_value_usd: float
    expected_buy_inventory_delta_usd: float
    expected_rebalancing_cost_usd: float
    expected_total_fees_usd: float
    expected_true_wealth_delta_usd: float | None
    would_trade: bool
    reason: str
    new_sell_pool: CostBasisPool | None
    new_buy_pool: CostBasisPool | None


def _fee_usd(amount: float, asset: str, quote_asset: str, price: float) -> float:
    return amount if asset == quote_asset else amount * price


def evaluate_arbitrage_true_economics(
    *,
    sell_pool: CostBasisPool,
    sell_qty: float,
    sell_price: float,
    sell_fee_amount: float,
    sell_fee_asset: str,
    buy_pool: CostBasisPool,
    buy_qty: float,
    buy_price: float,
    buy_fee_amount: float,
    buy_fee_asset: str,
    buy_side_mark_price: float,
    required_safety_margin_usd: float = 0.0,
    rebalance_impact_usd: float = 0.0,
    quote_asset: str = QUOTE_ASSET,
) -> TrueEconomicQuote:
    """Pure. The V5 replacement for execute_one_arbitrage's PNL-attribution
    formula. buy_side_mark_price is a conservative, independent valuation
    of the freshly-bought inventory (its own exchange's real BID, never
    the ask it was just bought at) -- this is what makes
    EXPECTED_BUY_INVENTORY_DELTA capture the real, immediate cost of
    crossing the spread instead of assuming a fresh buy is automatically
    worth what was paid for it. rebalance_impact_usd is the realized_pnl_
    usd from simulate_rebalance() when this trade requires one first
    (item 3), 0.0 otherwise -- already fee- and cost-basis-correct by
    construction, so it is added directly, never re-adjusted here."""
    sell_result = apply_sell(sell_pool, qty=sell_qty, price=sell_price, fee_amount=sell_fee_amount, fee_asset=sell_fee_asset, quote_asset=quote_asset)
    if sell_result is None:
        return TrueEconomicQuote(
            sell_inventory_cost_basis_usd=None, expected_net_sell_proceeds_usd=None, sell_side_realized_pnl_usd=None,
            new_buy_cost_usd=0.0, new_buy_mark_to_market_value_usd=0.0, expected_buy_inventory_delta_usd=0.0,
            expected_rebalancing_cost_usd=rebalance_impact_usd, expected_total_fees_usd=0.0,
            expected_true_wealth_delta_usd=None, would_trade=False,
            reason="SELL_COST_BASIS_UNKNOWN_OR_INSUFFICIENT_INVENTORY -- cannot value the units that would be sold, refusing to trade against a fabricated cost basis",
            new_sell_pool=None, new_buy_pool=None,
        )

    new_buy_pool = apply_buy(buy_pool, qty=buy_qty, price=buy_price, fee_amount=buy_fee_amount, fee_asset=buy_fee_asset, quote_asset=quote_asset)
    new_buy_cost_usd = buy_qty * buy_price + (buy_fee_amount if buy_fee_asset == quote_asset else 0.0)
    net_buy_qty = buy_qty - (buy_fee_amount if buy_fee_asset == buy_pool.asset else 0.0)
    new_buy_mark_to_market_value_usd = net_buy_qty * buy_side_mark_price
    expected_buy_inventory_delta_usd = new_buy_mark_to_market_value_usd - new_buy_cost_usd

    expected_total_fees_usd = (
        _fee_usd(sell_fee_amount, sell_fee_asset, quote_asset, sell_price)
        + _fee_usd(buy_fee_amount, buy_fee_asset, quote_asset, buy_price)
    )

    expected_true_wealth_delta_usd = sell_result.realized_pnl_usd + expected_buy_inventory_delta_usd + rebalance_impact_usd
    would_trade = expected_true_wealth_delta_usd > required_safety_margin_usd

    return TrueEconomicQuote(
        sell_inventory_cost_basis_usd=sell_result.cost_basis_of_units_sold_usd,
        expected_net_sell_proceeds_usd=sell_result.net_proceeds_usd,
        sell_side_realized_pnl_usd=sell_result.realized_pnl_usd,
        new_buy_cost_usd=new_buy_cost_usd,
        new_buy_mark_to_market_value_usd=new_buy_mark_to_market_value_usd,
        expected_buy_inventory_delta_usd=expected_buy_inventory_delta_usd,
        expected_rebalancing_cost_usd=rebalance_impact_usd,
        expected_total_fees_usd=expected_total_fees_usd,
        expected_true_wealth_delta_usd=expected_true_wealth_delta_usd,
        would_trade=would_trade,
        reason="expected_true_wealth_delta_usd > required_safety_margin_usd" if would_trade
        else f"expected_true_wealth_delta_usd={expected_true_wealth_delta_usd:.6f} <= required_safety_margin_usd={required_safety_margin_usd:.6f}",
        new_sell_pool=sell_result.pool, new_buy_pool=new_buy_pool,
    )


@dataclass(slots=True, frozen=True)
class InventoryConstitutionQuote:
    cost_usd: float
    mark_to_market_value_usd: float
    wealth_delta_usd: float
    pool_avg_cost_before: float | None
    already_underwater: bool
    would_constitute: bool
    reason: str
    new_pool: CostBasisPool


def evaluate_inventory_constitution_true_economics(
    pool: CostBasisPool, *, qty: float, ask_price: float, mark_price: float, fee_amount: float, fee_asset: str,
    required_safety_margin_usd: float = 0.0, quote_asset: str = QUOTE_ASSET,
) -> InventoryConstitutionQuote:
    """Pure. Item 4, user directive: "never treat inventory constitution
    as neutral by default." mark_price is the sell-side exchange's own
    real BID (where this inventory will eventually be sold) -- valuing
    the freshly-bought units there immediately, honestly, rather than
    assuming they are worth what was just paid (ask_price) for them.

    Item 4's "interdire les recyclages repetitifs qui produisent un faux
    edge tout en accumulant du stock economiquement defavorable" is
    operationalized as: the SAME true-economic gate applies here as to
    every other trade in this engine -- a buy is only justified if it
    does not immediately leave the pool worse off than the safety margin
    allows, applied uniformly rather than via a separate, arbitrary
    repetition counter. already_underwater is reported for visibility
    even when the buy is still allowed (e.g. a favorable price that
    improves a bad average), never used to silently block a trade the
    wealth-delta gate itself would allow."""
    cost_usd = qty * ask_price + (fee_amount if fee_asset == quote_asset else 0.0)
    net_qty = qty - (fee_amount if fee_asset == pool.asset else 0.0)
    mtm_value_usd = net_qty * mark_price
    wealth_delta_usd = mtm_value_usd - cost_usd
    pool_avg_cost_before = pool.avg_cost_per_unit
    already_underwater = pool_avg_cost_before is not None and pool_avg_cost_before > mark_price
    would_constitute = wealth_delta_usd > required_safety_margin_usd
    new_pool = apply_buy(pool, qty=qty, price=ask_price, fee_amount=fee_amount, fee_asset=fee_asset, quote_asset=quote_asset)
    return InventoryConstitutionQuote(
        cost_usd=cost_usd, mark_to_market_value_usd=mtm_value_usd, wealth_delta_usd=wealth_delta_usd,
        pool_avg_cost_before=pool_avg_cost_before, already_underwater=already_underwater, would_constitute=would_constitute,
        reason="wealth_delta_usd > required_safety_margin_usd" if would_constitute
        else f"wealth_delta_usd={wealth_delta_usd:.6f} <= required_safety_margin_usd={required_safety_margin_usd:.6f}",
        new_pool=new_pool,
    )
