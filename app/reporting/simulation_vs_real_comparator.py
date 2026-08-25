"""SIMULATION vs REAL MONEY OPPORTUNITY FORENSIC COMPARATOR (user
directive, 2026-08-25, "MISSION -- SIMULATION vs REAL MONEY OPPORTUNITY
FORENSIC COMPARISON"). Read-only, analysis-only: nothing in this module
imports an order-capable client (no live_arbitrage_executor, no
*_live_trade_client), and nothing here places, modifies, or cancels an
order. It only computes numbers from real, caller-supplied market data.

Feeds the SAME real (buy_leg, sell_leg) snapshot -- the identical
LegSnapshot pair app.execution.dual_leg_quote already consumes -- into
BOTH decision pipelines, so a comparison is never T1-simulation vs
T2-real:

  SIMULATION side: app.simulation.paper_trader's actual upstream pricing
  primitives (app.market_data.orderbook.simulate_vwap +
  app.analytics.fees.FeeEngine.trading_fee, the exact two functions
  app.analytics.execution_depth.evaluate_capital_tier composes), applied
  at the OPTIMAL depth-maximizing size
  (app.analytics.execution_depth.compute_depth_adjusted_edge) -- because
  that is genuinely what a liquidity-capped Opportunity's capital_usd/
  expected_profit_usd are priced at before paper_trader.simulate() ever
  sees them (app/opportunity/models.py:28-35). Deliberately EXCLUDES
  paper_trader.simulate()'s own RNG layers (EXECUTION_SLIPPAGE, 1.5%
  LEG_FAILURE_PROBABILITY) so this comparator stays a deterministic,
  reproducible function of real market data -- their real EXPECTED VALUE
  drag is reported alongside instead (SIM_EXPECTED_SLIPPAGE_DRAG_PCT),
  never silently injected as noise into a forensic comparison.

  REAL V5 side: app.execution.dual_leg_quote.compute_dual_leg_quote +
  app.execution.true_economic_pretrade.evaluate_arbitrage_true_economics
  -- unchanged, the exact real-money decision path used everywhere else
  in this project.

CONFIRMED (Phase 0 research, this mission): app.simulation.portfolios.
VirtualPortfolio is a single global USDT cash balance
(app/simulation/portfolios.py:15-35); paper_trader.simulate() only ever
credits portfolio.balances["USDT"] (app/simulation/paper_trader.py:221)
and caps capital by portfolio.available_usd(now) / current_value_usd --
never by whether the SELL exchange actually holds the asset being sold,
and never against a cost-basis pool. app.simulation.rebalancer.
RebalancingEngine.plan() is an unimplemented stub
(NotImplementedError, app/simulation/rebalancer.py:24-26) and is never
imported anywhere else in the codebase -- confirmed dead. This is
SIMULATION_INVENTORY_ACCOUNTING_BIAS = TRUE, structurally, by
construction, not a hypothesis this module needs to re-derive at
runtime -- recompute_sim_true_economic below is what quantifies its
real cost."""

import uuid
from dataclasses import dataclass

from app.analytics.execution_depth import CAPITAL_TEST_TIERS_USD, compute_depth_adjusted_edge
from app.analytics.fees import FeeEngine
from app.config.constants import MarketType
from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_dual_leg_quote
from app.execution.true_economic_ledger import CostBasisPool
from app.execution.true_economic_pretrade import ExecutabilityCheck, TrueEconomicQuote, evaluate_arbitrage_true_economics, evaluate_executability
from app.market_data.orderbook import OrderBookLevel, simulate_vwap

QUOTE_ASSET = "USDT"

# paper_trader.py's own real constants (app/simulation/paper_trader.py:37-44),
# reused here as disclosed, NOT randomly sampled -- see module docstring.
SIM_EXECUTION_SLIPPAGE_MEAN_PCT = -0.015
SIM_LEG_FAILURE_PROBABILITY = 0.015
SIM_EMERGENCY_UNWIND_COST_PCT = 0.25


def _levels(depth: list[tuple[float, float]]) -> list[OrderBookLevel]:
    return [OrderBookLevel(price=p, quantity=q) for p, q in depth]


@dataclass(slots=True, frozen=True)
class SimResult:
    detected: bool
    would_trade: bool
    notional_usd: float | None
    buy_qty: float | None
    sell_qty: float | None
    gross_pnl_usd: float | None
    fees_usd: float | None
    expected_slippage_drag_usd: float | None  # mean-case only, disclosed not sampled
    net_pnl_usd: float | None
    net_return_bps: float | None
    capital_required_usd: float | None
    inventory_required_qty: float | None  # always None -- SIM never models this, disclosed explicitly
    rejection_reason: str | None
    buy_avg_price: float | None
    sell_avg_price: float | None


def compute_sim_side(
    *, buy_exchange: str, sell_exchange: str, buy_leg: LegSnapshot, sell_leg: LegSnapshot,
    fee_engine: FeeEngine | None = None, test_tiers_usd: list[float] = CAPITAL_TEST_TIERS_USD,
) -> SimResult:
    """Pure. Faithfully reproduces what a liquidity-capped Opportunity's
    capital_usd/expected_profit_usd would be priced at, using the real
    upstream functions -- never a re-derived approximation."""
    fee_engine = fee_engine or FeeEngine()
    ask_levels, bid_levels = _levels(buy_leg.depth_levels), _levels(sell_leg.depth_levels)
    gross_spread_pct = (sell_leg.best_bid - buy_leg.best_ask) / buy_leg.best_ask * 100 if buy_leg.best_ask > 0 else 0.0
    detected = gross_spread_pct > 0  # top-of-book crossable spread -- the same signal a detector engine gates on before pricing anything

    edge = compute_depth_adjusted_edge(
        buy_exchange, sell_exchange, ask_levels, bid_levels, gross_spread_pct, fee_engine,
        intended_capital_usd=test_tiers_usd[0], test_tiers_usd=test_tiers_usd,
    )
    if edge.optimal_capital_usd is None or edge.optimal_net_profit_usd is None:
        return SimResult(
            detected=detected, would_trade=False, notional_usd=None, buy_qty=None, sell_qty=None,
            gross_pnl_usd=None, fees_usd=None, expected_slippage_drag_usd=None, net_pnl_usd=None,
            net_return_bps=None, capital_required_usd=None, inventory_required_qty=None,
            rejection_reason="NO_PROFITABLE_TIER_AT_ANY_TESTED_SIZE" if detected else "TOP_OF_BOOK_SPREAD_NOT_CROSSABLE",
            buy_avg_price=None, sell_avg_price=None,
        )

    notional = edge.optimal_capital_usd
    buy_fill = simulate_vwap(ask_levels, notional)
    sell_fill = simulate_vwap(bid_levels, notional)
    filled_usd = min(buy_fill.filled_usd, sell_fill.filled_usd)
    qty = filled_usd / buy_fill.average_price if buy_fill.average_price > 0 else 0.0
    buy_fee = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, filled_usd, is_maker=False)
    sell_notional = qty * sell_fill.average_price
    sell_fee = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, sell_notional, is_maker=False)
    gross_pnl = qty * (sell_fill.average_price - buy_fill.average_price)

    expected_slippage_drag = notional * (SIM_EXECUTION_SLIPPAGE_MEAN_PCT / 100)
    net_pnl = edge.optimal_net_profit_usd  # already gross - fees, from the real function; slippage/leg-failure NOT applied (disclosed)
    net_return_bps = (net_pnl / notional * 10000.0) if notional > 0 else None

    return SimResult(
        detected=detected, would_trade=net_pnl > 0, notional_usd=notional, buy_qty=qty, sell_qty=qty,
        gross_pnl_usd=gross_pnl, fees_usd=buy_fee + sell_fee, expected_slippage_drag_usd=expected_slippage_drag,
        net_pnl_usd=net_pnl, net_return_bps=net_return_bps, capital_required_usd=notional,
        inventory_required_qty=None,
        rejection_reason=None if net_pnl > 0 else f"net_pnl_usd={net_pnl:.6f} <= 0 at optimal size {notional:.2f}",
        buy_avg_price=buy_fill.average_price, sell_avg_price=sell_fill.average_price,
    )


@dataclass(slots=True, frozen=True)
class RealResult:
    detected: bool
    would_trade: bool
    max_executable_notional_usd: float
    common_qty: float
    gross_spread_pnl_usd: float
    fees_usd: float
    slippage_pct: float
    sell_side_cost_basis_usd: float | None
    sell_side_realized_pnl_usd: float | None
    buy_side_mark_to_market_delta_usd: float
    rebalance_impact_usd: float
    inventory_impact_usd: float  # sell_side_realized_pnl + buy_side_mtm_delta -- the two inventory-sensitive terms combined
    true_economic_pnl_usd: float | None
    true_economic_return_bps: float | None
    buy_balance_usd: float
    sell_inventory_qty: float
    reserve_floor_usd: float
    rejection_reason: str
    quote: DualLegQuote
    te_quote: TrueEconomicQuote


def compute_real_side(
    *, symbol: str, buy_exchange: str, sell_exchange: str, buy_leg: LegSnapshot, sell_leg: LegSnapshot,
    opportunity_id: uuid.UUID, sell_pool: CostBasisPool, buy_pool: CostBasisPool,
    real_buy_balance_usd: float, real_sell_inventory_qty: float, reserve_floor_usd: float,
    notional_usd: float, required_safety_margin_usd: float = 0.0,
) -> RealResult:
    """Pure. The unchanged real-money V5 decision path -- notional_usd is
    the CURRENTLY DEPLOYED fixed size (app.config.settings.
    max_notional_per_leg_usdt), not simulation's optimal size: real money
    is capped there today, and that gap is itself part of what this
    comparator measures (never silently equalized)."""
    quote = compute_dual_leg_quote(
        opportunity_id=opportunity_id, symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg,
        master_requested_size_usd=notional_usd, micro_live_cap_usdt=notional_usd,
    )
    detected = quote.gross_spread_pct > 0

    te_quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=quote.executable_qty, sell_price=quote.sell_execution_price,
        sell_fee_amount=quote.sell_fee_usd, sell_fee_asset=QUOTE_ASSET,
        buy_pool=buy_pool, buy_qty=quote.executable_qty, buy_price=quote.buy_execution_price,
        buy_fee_amount=quote.buy_fee_usd, buy_fee_asset=QUOTE_ASSET,
        buy_side_mark_price=buy_leg.best_bid, required_safety_margin_usd=required_safety_margin_usd,
    )
    capital_required = quote.executable_qty * quote.buy_execution_price
    capital_available = max(0.0, real_buy_balance_usd - reserve_floor_usd)
    executability: ExecutabilityCheck = evaluate_executability(
        capital_required_usd=capital_required, capital_available_usd=capital_available,
        inventory_required_qty=quote.executable_qty, inventory_available_qty=real_sell_inventory_qty,
        true_economic_positive=te_quote.would_trade,
    )

    inventory_impact = (te_quote.sell_side_realized_pnl_usd or 0.0) + te_quote.expected_buy_inventory_delta_usd
    gross_spread_pnl = quote.executable_qty * (quote.sell_execution_price - quote.buy_execution_price)

    if not quote.executable:
        reason = f"QUOTE_REJECTED: {quote.reason}"
    elif executability.blocker is not None:
        reason = executability.blocker
    elif not te_quote.would_trade:
        reason = te_quote.reason
    else:
        reason = "NONE_EXECUTABLE"

    return RealResult(
        detected=detected, would_trade=executability.executable_now,
        max_executable_notional_usd=capital_required, common_qty=quote.executable_qty,
        gross_spread_pnl_usd=gross_spread_pnl, fees_usd=quote.buy_fee_usd + quote.sell_fee_usd,
        slippage_pct=max(quote.buy_slippage_pct, quote.sell_slippage_pct),
        sell_side_cost_basis_usd=te_quote.sell_inventory_cost_basis_usd,
        sell_side_realized_pnl_usd=te_quote.sell_side_realized_pnl_usd,
        buy_side_mark_to_market_delta_usd=te_quote.expected_buy_inventory_delta_usd,
        rebalance_impact_usd=te_quote.expected_rebalancing_cost_usd, inventory_impact_usd=inventory_impact,
        true_economic_pnl_usd=te_quote.expected_true_wealth_delta_usd,
        true_economic_return_bps=(te_quote.expected_true_wealth_delta_usd / capital_required * 10000.0) if te_quote.expected_true_wealth_delta_usd is not None and capital_required > 0 else None,
        buy_balance_usd=real_buy_balance_usd, sell_inventory_qty=real_sell_inventory_qty, reserve_floor_usd=reserve_floor_usd,
        rejection_reason=reason, quote=quote, te_quote=te_quote,
    )


def recompute_sim_true_economic(
    *, sim: SimResult, sell_pool: CostBasisPool, buy_pool: CostBasisPool, buy_side_mark_price: float,
    required_safety_margin_usd: float = 0.0,
) -> float | None:
    """Pure. Phase 4 -- takes SIMULATION's OWN traded quantity/prices and
    runs them through the exact real true-economic gate against the
    REAL ledger's cost-basis pool for the sell exchange. This directly
    answers "if simulation's own opportunity were priced honestly against
    real inventory cost, what would it actually net" -- the single most
    important number in this mission. None if sim never priced a trade."""
    if sim.buy_qty is None or sim.sell_qty is None or sim.buy_avg_price is None or sim.sell_avg_price is None or sim.fees_usd is None:
        return None
    half_fee = sim.fees_usd / 2  # SimResult doesn't separately track buy/sell fee; split evenly, disclosed
    te_quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=sim.sell_qty, sell_price=sim.sell_avg_price,
        sell_fee_amount=half_fee, sell_fee_asset=QUOTE_ASSET,
        buy_pool=buy_pool, buy_qty=sim.buy_qty, buy_price=sim.buy_avg_price,
        buy_fee_amount=half_fee, buy_fee_asset=QUOTE_ASSET,
        buy_side_mark_price=buy_side_mark_price, required_safety_margin_usd=required_safety_margin_usd,
    )
    return te_quote.expected_true_wealth_delta_usd


DIFFERENCE_CATEGORIES = (
    "FALSE_SIMULATION_EDGE", "REAL_FEES_HIGHER", "REAL_SLIPPAGE_HIGHER", "REAL_DEPTH_INSUFFICIENT",
    "SELL_INVENTORY_MISSING", "SELL_COST_BASIS_UNKNOWN", "SELL_COST_BASIS_UNPROFITABLE", "MIN_NOTIONAL",
    "MIN_QTY", "STEP_SIZE", "INSUFFICIENT_BUY_CAPITAL", "RESERVE_FLOOR", "REBALANCE_COST", "INVENTORY_COST",
    "EDGE_DISAPPEARED", "SIMULATION_ASSUMED_IMPOSSIBLE_INVENTORY", "SIMULATION_ASSUMED_IMPOSSIBLE_CAPITAL",
    "SIMULATION_ACCOUNTING_BIAS", "OTHER",
)


@dataclass(slots=True, frozen=True)
class DifferenceAttribution:
    primary_cause: str
    secondary_causes: tuple[str, ...]


def classify_difference(
    *, sim: SimResult, real: RealResult, sim_recalculated_true_economic_pnl: float | None,
    real_sell_inventory_qty: float,
) -> DifferenceAttribution | None:
    """Pure. Only meaningful when SIM_WOULD_TRADE=True and REAL_WOULD_TRADE=False
    (mission Phase 3) -- returns None otherwise. May return several
    causes; PRIMARY is the first structural blocker in the same
    precedence order app.execution.rejection_classifier uses (a size/
    tradability failure precedes an economic one, which precedes a
    capital one), SECONDARY are the rest that also independently held."""
    if not (sim.would_trade and not real.would_trade):
        return None

    causes: list[str] = []

    if not real.quote.buy_lot_size_pass or not real.quote.sell_lot_size_pass:
        causes.append("MIN_QTY")
    if not real.quote.buy_min_notional_pass or not real.quote.sell_min_notional_pass:
        causes.append("MIN_NOTIONAL")
    if real.slippage_pct >= 100.0:
        causes.append("REAL_DEPTH_INSUFFICIENT")

    if real_sell_inventory_qty <= 1e-9:
        causes.append("SIMULATION_ASSUMED_IMPOSSIBLE_INVENTORY")
        causes.append("SELL_INVENTORY_MISSING")
    elif real.sell_side_cost_basis_usd is None:
        causes.append("SELL_COST_BASIS_UNKNOWN")
    elif real.common_qty < sim.sell_qty - 1e-9:
        causes.append("SELL_INVENTORY_MISSING")  # real inventory exists but less than sim assumed

    if sim_recalculated_true_economic_pnl is not None and sim_recalculated_true_economic_pnl <= 0 and sim.net_pnl_usd is not None and sim.net_pnl_usd > 0:
        causes.append("SIMULATION_ACCOUNTING_BIAS")
        if real.sell_side_cost_basis_usd is not None and (real.sell_side_realized_pnl_usd or 0.0) < 0:
            causes.append("SELL_COST_BASIS_UNPROFITABLE")

    if real.max_executable_notional_usd < (sim.notional_usd or 0.0) and real.buy_balance_usd - real.reserve_floor_usd < (sim.notional_usd or 0.0):
        causes.append("INSUFFICIENT_BUY_CAPITAL")
        causes.append("SIMULATION_ASSUMED_IMPOSSIBLE_CAPITAL")
        if real.buy_balance_usd >= (sim.notional_usd or 0.0) > real.buy_balance_usd - real.reserve_floor_usd:
            causes.append("RESERVE_FLOOR")

    if real.fees_usd > (sim.fees_usd or 0.0):
        causes.append("REAL_FEES_HIGHER")

    if real.rebalance_impact_usd < 0:
        causes.append("REBALANCE_COST")
    if real.buy_side_mark_to_market_delta_usd < 0:
        causes.append("INVENTORY_COST")

    if real.true_economic_pnl_usd is not None and real.true_economic_pnl_usd <= 0 and not causes:
        causes.append("FALSE_SIMULATION_EDGE")

    if not causes:
        causes.append("OTHER")

    # de-duplicate, preserve first-seen order (= precedence)
    seen: list[str] = []
    for c in causes:
        if c not in seen:
            seen.append(c)
    return DifferenceAttribution(primary_cause=seen[0], secondary_causes=tuple(seen[1:]))
