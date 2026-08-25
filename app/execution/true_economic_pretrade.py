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

import uuid
from dataclasses import dataclass

from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_dual_leg_quote
from app.execution.true_economic_ledger import CostBasisPool, apply_buy, apply_sell

QUOTE_ASSET = "USDT"
DEFAULT_ROUTE_TIERS_USDT: tuple[float, ...] = (10.0, 20.0, 50.0)


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


def compute_return_bps(wealth_delta_usd: float | None, notional_usd: float) -> float | None:
    """Pure. TRUE_ECONOMIC_RETURN_BPS -- wealth_delta as basis points of
    the capital actually deployed on the buy leg. None (never a
    fabricated 0) when wealth_delta_usd is None (unknown cost basis) or
    notional_usd is not positive (nothing was actually deployed)."""
    if wealth_delta_usd is None or notional_usd <= 0:
        return None
    return wealth_delta_usd / notional_usd * 10000.0


@dataclass(slots=True, frozen=True)
class ExecutabilityCheck:
    """Item 'TRUE_ECONOMIC_POSITIVE vs EXECUTABLE_WITH_CURRENT_CAPITAL'
    (user directive, 2026-08-25, three-exchange V5 shadow): these are two
    DIFFERENT questions, deliberately kept separate. true_economic_
    positive is a pure statement about the trade's economics (does the
    complete operation increase real net worth) -- it says nothing about
    whether the capital/inventory to actually DO it exists right now.
    executable_now is the AND of both: economically justified AND
    actually fundable with real, current balances. A route can be
    TRUE_ECONOMIC_POSITIVE=True and EXECUTABLE_NOW=False purely because
    one exchange (e.g. an unfunded OKX account) doesn't have the capital
    yet -- that must remain visible, not silently dropped."""

    capital_required_usd: float
    capital_available_usd: float
    inventory_required_qty: float
    inventory_available_qty: float
    capital_sufficient: bool
    inventory_sufficient: bool
    true_economic_positive: bool
    executable_now: bool
    blocker: str | None


def evaluate_executability(
    *, capital_required_usd: float, capital_available_usd: float, inventory_required_qty: float,
    inventory_available_qty: float, true_economic_positive: bool,
) -> ExecutabilityCheck:
    """Pure. Combines the economic verdict (computed elsewhere, by
    evaluate_arbitrage_true_economics) with a real-capital/inventory
    availability check to produce EXECUTABLE_NOW and a single, specific
    BLOCKER reason -- never more than one cause conflated into a vague
    'not executable'."""
    capital_sufficient = capital_available_usd >= capital_required_usd
    inventory_sufficient = inventory_available_qty >= inventory_required_qty
    executable_now = true_economic_positive and capital_sufficient and inventory_sufficient

    if not true_economic_positive:
        blocker = "NOT_TRUE_ECONOMIC_POSITIVE"
    elif not capital_sufficient and not inventory_sufficient:
        blocker = "INSUFFICIENT_CAPITAL_AND_INVENTORY"
    elif not capital_sufficient:
        blocker = "INSUFFICIENT_CAPITAL"
    elif not inventory_sufficient:
        blocker = "INSUFFICIENT_INVENTORY"
    else:
        blocker = None

    return ExecutabilityCheck(
        capital_required_usd=capital_required_usd, capital_available_usd=capital_available_usd,
        inventory_required_qty=inventory_required_qty, inventory_available_qty=inventory_available_qty,
        capital_sufficient=capital_sufficient, inventory_sufficient=inventory_sufficient,
        true_economic_positive=true_economic_positive, executable_now=executable_now, blocker=blocker,
    )


@dataclass(slots=True, frozen=True)
class TieredTrueEconomicResult:
    tier_usdt: float
    quote: DualLegQuote
    true_economic: TrueEconomicQuote


def evaluate_route_across_tiers(
    *, symbol: str, buy_leg: LegSnapshot, sell_leg: LegSnapshot, opportunity_id: uuid.UUID,
    sell_pool: CostBasisPool, buy_pool: CostBasisPool, required_safety_margin_usd: float = 0.0,
    rebalance_impact_usd: float = 0.0, tiers_usdt: tuple[float, ...] = DEFAULT_ROUTE_TIERS_USDT,
) -> list[TieredTrueEconomicResult]:
    """Pure. EXPECTED_TRUE_PNL @ $10/$20/$50 (user directive) -- reuses
    ONE real market snapshot (buy_leg/sell_leg, fetched once by the
    caller) across every tier, exactly like app.execution.
    capital_scaling_observer.observe_at_tiers, so real depth/slippage at
    each size is captured honestly rather than linearly scaled from a
    single quote. opportunity_id is caller-supplied (this stays pure --
    no uuid4()/clock reads here)."""
    results = []
    for tier in tiers_usdt:
        quote = compute_dual_leg_quote(
            opportunity_id=opportunity_id, symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg,
            master_requested_size_usd=tier, micro_live_cap_usdt=tier,
        )
        te_quote = evaluate_arbitrage_true_economics(
            sell_pool=sell_pool, sell_qty=quote.executable_qty, sell_price=quote.sell_execution_price,
            sell_fee_amount=quote.sell_fee_usd, sell_fee_asset=QUOTE_ASSET,
            buy_pool=buy_pool, buy_qty=quote.executable_qty, buy_price=quote.buy_execution_price,
            buy_fee_amount=quote.buy_fee_usd, buy_fee_asset=QUOTE_ASSET,
            buy_side_mark_price=buy_leg.best_bid, required_safety_margin_usd=required_safety_margin_usd,
            rebalance_impact_usd=rebalance_impact_usd,
        )
        results.append(TieredTrueEconomicResult(tier_usdt=tier, quote=quote, true_economic=te_quote))
    return results


@dataclass(slots=True, frozen=True)
class RouteObservation:
    """One fully-evaluated (symbol, buy_exchange, sell_exchange)
    direction, at the natural/default size plus the 3 reference tiers --
    everything the V5 three-exchange shadow report needs for a single
    route, composed from the pure functions above so the shadow script
    itself stays thin orchestration (fetch real data, call this, log the
    result)."""

    symbol: str
    buy_exchange: str
    sell_exchange: str
    old_edge_usd: float
    true_economic_edge_usd: float | None
    true_economic_return_bps: float | None
    expected_true_pnl_by_tier_usd: dict[float, float | None]
    capital_required_per_exchange_usd: dict[str, float]
    inventory_required: dict[str, dict[str, float]]  # {exchange: {asset: qty}}
    executability: ExecutabilityCheck
    true_economic_quote: TrueEconomicQuote


def build_route_observation(
    *,
    symbol: str,
    base_asset: str,
    buy_exchange: str,
    sell_exchange: str,
    buy_leg: LegSnapshot,
    sell_leg: LegSnapshot,
    opportunity_id: uuid.UUID,
    sell_pool: CostBasisPool,
    buy_pool: CostBasisPool,
    available_usdt: dict[str, float],
    available_base: dict[str, float],
    required_safety_margin_usd: float = 0.0,
    rebalance_impact_usd: float = 0.0,
    natural_size_usdt: float = 10.0,
    tiers_usdt: tuple[float, ...] = DEFAULT_ROUTE_TIERS_USDT,
) -> RouteObservation:
    """Pure. available_usdt/available_base are real, currently-held
    balances keyed by exchange name -- TRUE_ECONOMIC_POSITIVE (the
    economic verdict, from true_economic_quote.would_trade) and
    EXECUTABLE_NOW (economic verdict AND real capital/inventory
    available) are computed independently and both surfaced, per the
    user's explicit requirement: an opportunity can be excellent and
    still unexecutable purely for lack of funding on one exchange, and
    that must remain visible, not conflated into one boolean."""
    natural_quote = compute_dual_leg_quote(
        opportunity_id=opportunity_id, symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg,
        master_requested_size_usd=natural_size_usdt, micro_live_cap_usdt=natural_size_usdt,
    )
    te_quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=natural_quote.executable_qty, sell_price=natural_quote.sell_execution_price,
        sell_fee_amount=natural_quote.sell_fee_usd, sell_fee_asset=QUOTE_ASSET,
        buy_pool=buy_pool, buy_qty=natural_quote.executable_qty, buy_price=natural_quote.buy_execution_price,
        buy_fee_amount=natural_quote.buy_fee_usd, buy_fee_asset=QUOTE_ASSET,
        buy_side_mark_price=buy_leg.best_bid, required_safety_margin_usd=required_safety_margin_usd,
        rebalance_impact_usd=rebalance_impact_usd,
    )
    capital_required_usd = natural_quote.executable_qty * natural_quote.buy_execution_price
    return_bps = compute_return_bps(te_quote.expected_true_wealth_delta_usd, capital_required_usd)

    tiered = evaluate_route_across_tiers(
        symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg, opportunity_id=opportunity_id,
        sell_pool=sell_pool, buy_pool=buy_pool, required_safety_margin_usd=required_safety_margin_usd,
        rebalance_impact_usd=rebalance_impact_usd, tiers_usdt=tiers_usdt,
    )
    expected_true_pnl_by_tier = {r.tier_usdt: r.true_economic.expected_true_wealth_delta_usd for r in tiered}

    executability = evaluate_executability(
        capital_required_usd=capital_required_usd, capital_available_usd=available_usdt.get(buy_exchange, 0.0),
        inventory_required_qty=natural_quote.executable_qty, inventory_available_qty=available_base.get(sell_exchange, 0.0),
        true_economic_positive=te_quote.would_trade,
    )

    return RouteObservation(
        symbol=symbol, buy_exchange=buy_exchange, sell_exchange=sell_exchange, old_edge_usd=natural_quote.net_profit_usd,
        true_economic_edge_usd=te_quote.expected_true_wealth_delta_usd, true_economic_return_bps=return_bps,
        expected_true_pnl_by_tier_usd=expected_true_pnl_by_tier,
        capital_required_per_exchange_usd={buy_exchange: capital_required_usd},
        inventory_required={sell_exchange: {base_asset: natural_quote.executable_qty}},
        executability=executability, true_economic_quote=te_quote,
    )
