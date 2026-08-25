"""TRUE ECONOMIC LEDGER (user directive, 2026-08-25, V5 -- "TRUE ECONOMIC
ARBITRAGE ENGINE"). Replaces app.reporting.live_trading_dashboard's
compute_cost_basis_by_asset_exchange, which was found (forensic
reconstruction, same date) to accumulate every BUY ever recorded into a
lifetime running average that is NEVER decremented when units are sold --
by the 139th rebalance of the V4 session this produced a "cost basis"
averaged across the whole session's history rather than the true
remaining cost of currently-held inventory, corrupting every downstream
realized-PNL calculation that consumed it.

This module is the single source of truth for "what did the units I am
about to sell actually cost": a weighted-average, chronologically-
DEPLETING cost-basis pool per (exchange, asset). Every BUY increases
quantity and cost. Every SELL removes EXACTLY the quantity sold (net of
any base-asset fee) from the pool and realizes
PROCEEDS - COST_BASIS_OF_THE_UNITS_SOLD -- never a same-cycle
cross-exchange notional match (the V4 bug in
app.execution.live_arbitrage_executor.execute_one_arbitrage, which
compared THIS cycle's buy-exchange fill cost against THIS cycle's
sell-exchange fill proceeds as though they were the same trade, when the
sell leg actually draws down a separately-accumulated pool on a
different exchange).

Fee currency handling (matches the forensic reconstruction exactly, which
independently closed a 606-real-fill wealth bridge to $0.000000 using
this exact mechanic): a fee paid in the BASE asset reduces the net
quantity that moves (it never touched the USD cost/proceeds figure,
since it was never converted to USD at trade time); a fee paid in the
QUOTE asset (USDT) increases cost on a BUY and reduces proceeds on a
SELL. Never both for the same fee -- that would double-count exactly the
way a prior, already-fixed bug once did (see
live_arbitrage_executor.py's own FIX 1 comment).

Pure core (CostBasisPool, apply_buy, apply_sell -- no I/O, no clock
reads, every timestamp/price/qty is caller-supplied); load_state/
save_state are the only I/O, isolated at the edges, matching every other
app.operations persistence module (order_intent_log.py,
persistent_kill_switch.py)."""

import json
from dataclasses import asdict, dataclass, replace
from pathlib import Path

DEFAULT_STATE_PATH = Path("/opt/robotcripto/data/true_economic_ledger.json")


@dataclass(slots=True, frozen=True)
class CostBasisPool:
    exchange: str
    asset: str
    qty: float
    cost_usd: float  # total USD cost basis of `qty` units, i.e. qty * avg_cost_per_unit

    @property
    def avg_cost_per_unit(self) -> float | None:
        """None (never a fabricated 0.0) when the pool is empty -- matches
        app.execution.capital_rebalancer.compute_rebalance_realized_pnl's
        own "never a fabricated number" convention, so a caller with an
        empty pool is forced to handle the unknown-cost-basis case
        explicitly rather than silently trading against a $0 basis."""
        if self.qty <= 1e-12:
            return None
        return self.cost_usd / self.qty


@dataclass(slots=True, frozen=True)
class SellResult:
    pool: CostBasisPool
    realized_pnl_usd: float
    cost_basis_of_units_sold_usd: float
    net_proceeds_usd: float
    net_qty_sold: float


def empty_pool(exchange: str, asset: str) -> CostBasisPool:
    return CostBasisPool(exchange=exchange, asset=asset, qty=0.0, cost_usd=0.0)


def apply_buy(
    pool: CostBasisPool, *, qty: float, price: float, fee_amount: float, fee_asset: str, quote_asset: str = "USDT",
) -> CostBasisPool:
    """Pure. Returns a new pool -- never mutates the input. qty/price are
    the real fill's own qty and average fill price; fee_amount/fee_asset
    the real commission actually charged on that fill."""
    if qty <= 0:
        return pool
    net_qty_in = qty - (fee_amount if fee_asset == pool.asset else 0.0)
    cost_usd_in = qty * price + (fee_amount if fee_asset == quote_asset else 0.0)
    return replace(pool, qty=pool.qty + net_qty_in, cost_usd=pool.cost_usd + cost_usd_in)


def apply_sell(
    pool: CostBasisPool, *, qty: float, price: float, fee_amount: float, fee_asset: str, quote_asset: str = "USDT",
) -> SellResult | None:
    """Pure. Returns None (never a fabricated cost basis) if the pool
    cannot cover the quantity being sold -- an empty/insufficient pool
    means the true acquisition cost of these specific units is unknown,
    and no economic decision should be made against an invented number.
    The caller (app.execution.true_economic_pretrade) treats None as
    "do not trade," matching this project's standing convention."""
    if qty <= 0:
        return None
    net_qty_out = qty + (fee_amount if fee_asset == pool.asset else 0.0)
    avg_cost = pool.avg_cost_per_unit
    if avg_cost is None or net_qty_out > pool.qty + 1e-9:
        return None
    proceeds_usd = qty * price - (fee_amount if fee_asset == quote_asset else 0.0)
    cost_removed = net_qty_out * avg_cost
    realized_pnl = proceeds_usd - cost_removed
    new_pool = replace(pool, qty=pool.qty - net_qty_out, cost_usd=pool.cost_usd - cost_removed)
    return SellResult(
        pool=new_pool, realized_pnl_usd=realized_pnl, cost_basis_of_units_sold_usd=cost_removed,
        net_proceeds_usd=proceeds_usd, net_qty_sold=net_qty_out,
    )


LedgerState = dict[str, CostBasisPool]  # key: f"{exchange}|{asset}"


def _key(exchange: str, asset: str) -> str:
    return f"{exchange}|{asset}"


def get_pool(state: LedgerState, exchange: str, asset: str) -> CostBasisPool:
    return state.get(_key(exchange, asset), empty_pool(exchange, asset))


def put_pool(state: LedgerState, pool: CostBasisPool) -> LedgerState:
    """Pure. Returns a new state, never mutates the input."""
    return {**state, _key(pool.exchange, pool.asset): pool}


def seed_pool(state: LedgerState, exchange: str, asset: str, *, qty: float, price: float) -> LedgerState:
    """Pure. Initializes a pool from a known starting quantity/price --
    used once, at session start (or ledger-creation time), to seed
    pre-existing inventory with a real historical cost basis. Never call
    this on a pool that already has activity; it overwrites."""
    return put_pool(state, CostBasisPool(exchange=exchange, asset=asset, qty=qty, cost_usd=qty * price))


def load_state(path: Path = DEFAULT_STATE_PATH) -> LedgerState:
    if not path.exists():
        return {}
    with open(path) as f:
        raw = json.load(f)
    return {key: CostBasisPool(**entry) for key, entry in raw.items()}


def save_state(state: LedgerState, path: Path = DEFAULT_STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump({key: asdict(pool) for key, pool in state.items()}, f, indent=2, sort_keys=True)
