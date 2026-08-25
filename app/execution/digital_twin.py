"""V5 DIGITAL TWIN SIMULATION (user directive, 2026-08-25, "MISSION --
BUILD V5 DIGITAL TWIN SIMULATION"). Replaces app.simulation.paper_trader's
single global USDT cash balance with a real per-(exchange, asset)
portfolio, built ENTIRELY by composing the same pure functions the real
V5 engine already uses -- nothing here is a new pricing/accounting
formula, only new orchestration around existing, tested ones:

  scanner / regime / VWAP / depth / quality filters -- UNCHANGED, this
  module never touches them (per the user's explicit instruction).

  cost basis, per (exchange, asset) -- app.execution.true_economic_ledger.
  LedgerState/CostBasisPool/apply_buy/apply_sell (the SAME ledger V5
  uses, not a parallel implementation).

  sizing, real depth/slippage/fees -- app.execution.dual_leg_quote.
  compute_dual_leg_quote, app.execution.live_arbitrage_executor.
  compute_common_dual_leg_qty (capped by the TWIN's own simulated
  sell-side balance, exactly as compute_common_dual_leg_qty already
  caps by a real balance -- the twin's balance simply stands in for it).

  true-economic gate -- app.execution.true_economic_pretrade.
  evaluate_arbitrage_true_economics (identical call, identical fields:
  SELL_SIDE_REALIZED_PNL, BUY_SIDE_MARK_TO_MARKET_DELTA, TRUE_ECONOMIC_PNL).

  reserve floors -- app.execution.capital_rebalancer.compute_reserve_floor/
  evaluate_reserve_impact (identical calls).

  inventory constitution -- app.execution.true_economic_pretrade.
  evaluate_inventory_constitution_true_economics (identical call).

  rebalancing -- app.execution.true_economic_pretrade.simulate_rebalance
  (identical call).

  liquidation net worth -- app.reporting.real_net_worth.
  compute_liquidation_net_worth (identical call, applied to the twin's
  own simulated balances instead of a real account read).

THE ONE NEW RULE THIS MODULE ADDS: a sell is only ever attempted against
the TWIN's own tracked pool quantity -- never a real account balance,
never an assumption. If the pool holds nothing (or not enough), the
result is INVENTORY_MISSING, exactly the same shape as V5's own
SELL_COST_BASIS_UNKNOWN_OR_INSUFFICIENT_INVENTORY outcome (apply_sell
returning None) -- because it IS that same function, called against
different but equally real state. No asset is ever created except
through a simulated BUY (arbitrage buy leg or inventory constitution),
each costed at its own real ask price. This is what makes the twin
economically comparable to V5: both refuse to sell what neither
genuinely holds."""

import uuid
from dataclasses import dataclass

from app.execution.capital_rebalancer import ReserveImpact, compute_reserve_floor, evaluate_reserve_impact
from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_common_dual_leg_qty, compute_dual_leg_quote
from app.execution.true_economic_ledger import CostBasisPool, LedgerState, apply_buy, get_pool, put_pool, seed_pool
from app.execution.true_economic_pretrade import (
    InventoryConstitutionQuote, RebalanceSimulation, TrueEconomicQuote,
    evaluate_arbitrage_true_economics, evaluate_inventory_constitution_true_economics, simulate_rebalance,
)
from app.reporting.real_net_worth import compute_liquidation_net_worth

QUOTE_ASSET = "USDT"


@dataclass(slots=True, frozen=True)
class DigitalTwinState:
    """Immutable -- every mutating function below returns a NEW state,
    matching this whole codebase's pure-function convention (CostBasisPool,
    LedgerState etc. are never mutated in place either)."""

    usdt: dict[str, float]  # {exchange: usdt_balance} -- separate per exchange, never pooled
    ledger: LedgerState  # {"exchange|asset": CostBasisPool} -- the SAME structure V5's own ledger uses


def bootstrap_mode_a(real_usdt_by_exchange: dict[str, float], real_balances_by_exchange: dict[str, dict[str, float]], real_prices_by_exchange: dict[str, dict[str, float]]) -> DigitalTwinState:
    """MODE A -- bootstrap from real current balances (all three
    exchanges). Non-USDT balances are seeded into the ledger at CURRENT
    market price (matching every other real-money session-start seed in
    this codebase, e.g. continuous_live_session_v5_okx_mission.py's own
    seed_ledger): this establishes an honest $0 unrealized-PNL baseline
    going forward, not a claim about historical acquisition cost."""
    ledger: LedgerState = {}
    for exchange, balances in real_balances_by_exchange.items():
        prices = real_prices_by_exchange.get(exchange, {})
        for asset, qty in balances.items():
            if asset == QUOTE_ASSET or qty <= 0:
                continue
            price = prices.get(asset)
            if price is None:
                continue
            ledger = seed_pool(ledger, exchange, asset, qty=qty, price=price)
    return DigitalTwinState(usdt=dict(real_usdt_by_exchange), ledger=ledger)


def bootstrap_mode_b(capital_usdt: float, allocation_fraction_by_exchange: dict[str, float]) -> DigitalTwinState:
    """MODE B -- what-if capital, explicitly allocated across exchanges
    (allocation_fraction_by_exchange must sum to ~1.0). NEVER seeds any
    inventory -- "le what-if ne doit jamais supposer inventaire
    illimite" -- every exchange starts with an EMPTY ledger; any
    inventory the twin later holds only exists because a simulated BUY
    (arbitrage or inventory constitution) actually paid real ask-price
    USDT for it."""
    total_fraction = sum(allocation_fraction_by_exchange.values())
    usdt = {ex: capital_usdt * (frac / total_fraction) for ex, frac in allocation_fraction_by_exchange.items()}
    return DigitalTwinState(usdt=usdt, ledger={})


def compute_simulated_liquidation_net_worth(state: DigitalTwinState, current_prices_by_exchange: dict[str, dict[str, float]]) -> float:
    """SIMULATED LIQUIDATION NET WORTH -- the twin's primary KPI, reusing
    compute_liquidation_net_worth unchanged (the same real function V5's
    own real_net_worth reporting uses) applied per exchange to the
    twin's own simulated balances, then summed."""
    total = 0.0
    for exchange, usdt_balance in state.usdt.items():
        balances: dict[str, float] = {QUOTE_ASSET: usdt_balance}
        for key, pool in state.ledger.items():
            pool_exchange, asset = key.split("|", 1)
            if pool_exchange == exchange and pool.qty > 0:
                balances[asset] = pool.qty
        prices = current_prices_by_exchange.get(exchange, {})
        total += compute_liquidation_net_worth(balances, prices)
    return total


@dataclass(slots=True, frozen=True)
class TwinArbitrageResult:
    accepted: bool
    blocker: str | None  # "INVENTORY_MISSING" | None (accepted) | te_quote.reason (economically rejected)
    common_qty: float
    quote: DualLegQuote | None
    te_quote: TrueEconomicQuote | None
    new_state: DigitalTwinState  # == the input state, unchanged, when not accepted


def attempt_arbitrage_on_twin(
    state: DigitalTwinState, *, buy_exchange: str, sell_exchange: str, base_asset: str,
    buy_leg: LegSnapshot, sell_leg: LegSnapshot, reserve_floor_usd: float, notional_usd: float,
    required_safety_margin_usd: float = 0.0,
) -> TwinArbitrageResult:
    """The one new rule this module adds, stated in the module docstring:
    a sell is only ever attempted against what the TWIN's own ledger
    pool actually holds. Mirrors live_arbitrage_executor.execute_one_
    arbitrage's real shape (fetch quote -> cap by real/simulated
    inventory -> check reserve floor -> true-economic gate -> apply)
    exactly, just applied to simulated state instead of real orders."""
    sell_pool = get_pool(state.ledger, sell_exchange, base_asset)
    if sell_pool.qty <= 1e-9:
        return TwinArbitrageResult(accepted=False, blocker="INVENTORY_MISSING", common_qty=0.0, quote=None, te_quote=None, new_state=state)

    quote = compute_dual_leg_quote(
        opportunity_id=uuid.uuid4(), symbol=f"{base_asset}/{QUOTE_ASSET}", buy_leg=buy_leg, sell_leg=sell_leg,
        master_requested_size_usd=notional_usd, micro_live_cap_usdt=notional_usd,
    )
    if not quote.executable or quote.executable_qty <= 0:
        return TwinArbitrageResult(accepted=False, blocker=quote.reason or "QUOTE_NOT_EXECUTABLE", common_qty=0.0, quote=quote, te_quote=None, new_state=state)

    common_qty = compute_common_dual_leg_qty(quote.executable_qty, sell_pool.qty, buy_leg.step_size, sell_leg.step_size)
    if common_qty <= 0:
        return TwinArbitrageResult(accepted=False, blocker="INVENTORY_MISSING", common_qty=0.0, quote=quote, te_quote=None, new_state=state)
    fee_scale = common_qty / quote.executable_qty

    buy_notional = common_qty * quote.buy_execution_price
    impact: ReserveImpact = evaluate_reserve_impact(state.usdt.get(buy_exchange, 0.0), reserve_floor_usd, buy_notional)
    if impact.would_breach:
        return TwinArbitrageResult(accepted=False, blocker=f"RESERVE_FLOOR_WOULD_BREACH (shortfall {impact.shortfall_usdt:.4f} USDT)", common_qty=common_qty, quote=quote, te_quote=None, new_state=state)

    buy_pool = get_pool(state.ledger, buy_exchange, base_asset)
    te_quote = evaluate_arbitrage_true_economics(
        sell_pool=sell_pool, sell_qty=common_qty, sell_price=quote.sell_execution_price,
        sell_fee_amount=quote.sell_fee_usd * fee_scale, sell_fee_asset=QUOTE_ASSET,
        buy_pool=buy_pool, buy_qty=common_qty, buy_price=quote.buy_execution_price,
        buy_fee_amount=quote.buy_fee_usd * fee_scale, buy_fee_asset=QUOTE_ASSET,
        buy_side_mark_price=buy_leg.best_bid, required_safety_margin_usd=required_safety_margin_usd,
    )
    if not te_quote.would_trade or te_quote.new_sell_pool is None or te_quote.new_buy_pool is None:
        return TwinArbitrageResult(accepted=False, blocker=te_quote.reason, common_qty=common_qty, quote=quote, te_quote=te_quote, new_state=state)

    new_ledger = put_pool(put_pool(state.ledger, te_quote.new_sell_pool), te_quote.new_buy_pool)
    new_usdt = dict(state.usdt)
    new_usdt[buy_exchange] = new_usdt.get(buy_exchange, 0.0) - te_quote.new_buy_cost_usd
    new_usdt[sell_exchange] = new_usdt.get(sell_exchange, 0.0) + (te_quote.expected_net_sell_proceeds_usd or 0.0)
    new_state = DigitalTwinState(usdt=new_usdt, ledger=new_ledger)

    return TwinArbitrageResult(accepted=True, blocker=None, common_qty=common_qty, quote=quote, te_quote=te_quote, new_state=new_state)


@dataclass(slots=True, frozen=True)
class TwinConstitutionResult:
    accepted: bool
    reason: str
    quote: InventoryConstitutionQuote | None
    new_state: DigitalTwinState


def attempt_inventory_constitution_on_twin(
    state: DigitalTwinState, *, exchange: str, asset: str, qty: float, ask_price: float, mark_price: float,
    fee_amount: float, fee_asset: str = QUOTE_ASSET, required_safety_margin_usd: float = 0.0,
) -> TwinConstitutionResult:
    """Simulates BUYING inventory (real ask price, real fee) so a future
    sell on this exchange has something real to draw from -- never
    conjures the asset for free. Reuses evaluate_inventory_constitution_
    true_economics unchanged, so a constitution the twin accepts is
    exactly one V5 itself would accept (same wealth-delta gate)."""
    pool = get_pool(state.ledger, exchange, asset)
    cost_usd = qty * ask_price + (fee_amount if fee_asset == QUOTE_ASSET else 0.0)
    if state.usdt.get(exchange, 0.0) < cost_usd:
        return TwinConstitutionResult(accepted=False, reason=f"insufficient simulated USDT on {exchange} ({state.usdt.get(exchange, 0.0):.4f} < {cost_usd:.4f})", quote=None, new_state=state)

    quote = evaluate_inventory_constitution_true_economics(
        pool, qty=qty, ask_price=ask_price, mark_price=mark_price, fee_amount=fee_amount, fee_asset=fee_asset,
        required_safety_margin_usd=required_safety_margin_usd,
    )
    if not quote.would_constitute:
        return TwinConstitutionResult(accepted=False, reason=quote.reason, quote=quote, new_state=state)

    new_ledger = put_pool(state.ledger, quote.new_pool)
    new_usdt = dict(state.usdt)
    new_usdt[exchange] = new_usdt.get(exchange, 0.0) - cost_usd
    return TwinConstitutionResult(accepted=True, reason=quote.reason, quote=quote, new_state=DigitalTwinState(usdt=new_usdt, ledger=new_ledger))


@dataclass(slots=True, frozen=True)
class TwinRebalanceResult:
    performed: bool
    reason: str
    realized_pnl_usd: float | None
    new_state: DigitalTwinState


def maybe_rebalance_on_twin(
    state: DigitalTwinState, *, exchange: str, reserve_floor_usd: float, asset: str, sell_price: float,
    fee_rate: float, upcoming_trade_notional_usd: float = 0.0,
) -> TwinRebalanceResult:
    """CAPITAL REBALANCING -- if `exchange` would be under its reserve
    floor for an upcoming trade of upcoming_trade_notional_usd, sells
    just enough of ITS OWN held `asset` (never more than the shortfall,
    matching capital_rebalancer.simulate_event_sequence's own real
    behavior) back to USDT via simulate_rebalance, reusing the exact
    real cost-basis-depleting sell/realized-PNL mechanic."""
    impact = evaluate_reserve_impact(state.usdt.get(exchange, 0.0), reserve_floor_usd, upcoming_trade_notional_usd)
    if not impact.would_breach:
        return TwinRebalanceResult(performed=False, reason="already above reserve floor for this trade size", realized_pnl_usd=None, new_state=state)

    pool = get_pool(state.ledger, exchange, asset)
    if pool.qty <= 1e-9:
        return TwinRebalanceResult(performed=False, reason=f"{exchange} holds no {asset} to rebalance with", realized_pnl_usd=None, new_state=state)

    qty_to_sell = min(impact.shortfall_usdt / sell_price if sell_price > 0 else 0.0, pool.qty)
    fee_amount = qty_to_sell * sell_price * fee_rate
    sim: RebalanceSimulation | None = simulate_rebalance(pool, qty_to_sell=qty_to_sell, price=sell_price, fee_amount=fee_amount, fee_asset=QUOTE_ASSET)
    if sim is None:
        return TwinRebalanceResult(performed=False, reason="pool could not cover the intended rebalance quantity", realized_pnl_usd=None, new_state=state)

    new_ledger = put_pool(state.ledger, sim.new_pool)
    new_usdt = dict(state.usdt)
    new_usdt[exchange] = new_usdt.get(exchange, 0.0) + sim.net_proceeds_usd
    return TwinRebalanceResult(performed=True, reason="rebalanced to restore reserve floor headroom", realized_pnl_usd=sim.realized_pnl_usd, new_state=DigitalTwinState(usdt=new_usdt, ledger=new_ledger))
