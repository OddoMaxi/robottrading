"""Multi-Hop / DEX Triangular Arbitrage (Multi-Market Opportunity Engine,
V5.5, spec sections 6, 7).

Tokens = nodes, pools = edges (spec section 6). Builds a directed graph
from a chain's discovered pools and searches for profitable cycles that
start and end at a base asset (a stablecoin, matching CEX triangular's own
convention), up to MAX_ROUTE_LENGTH hops (default 3 — a triangular loop;
allowing 4 is exposed as an explicit parameter, not a silent default, per
the spec's own "Allow 4 only if testing proves useful"). Combinatorial
explosion isn't a real risk at this scale — the discovered-pool graph per
chain has on the order of a handful to a few dozen edges (pool_discovery's
own liquidity/safety gates already keep it small), so a plain depth-first
search with a "never revisit a token mid-path" prune is enough; a real
combinatorial-explosion problem (thousands of pools) would need proper
graph pruning heuristics, not built here since the input scale doesn't
require it yet.

Simulation, section 7's own field list (input amount, swap 1/2/3 output,
fees, gas, price impact, slippage, final output) — walks the loop tracking
BOTH a running quantity and a running USD-price-of-the-currently-held-asset,
mirroring app.engines.triangular's own proven-correct CEX approach exactly
(see that module's docstring: "walk the loop once, tracking the running
quantity ... and that asset's own USD price"). This is deliberately NOT
the same USD-value-pass-through app.onchain.cross_dex_arbitrage.estimate_amm_output_usd
was originally (and incorrectly) used for on its own — a bug caught
building THIS module while sanity-checking a concrete scenario against
scratch: applying estimate_amm_output_usd alone at every hop, without
converting through each hop's REAL price, is dollar-value-invariant BY
CONSTRUCTION regardless of whether the loop's real rates are internally
consistent — it can never detect a genuine triangular mispricing, only
fees/impact losses, because dollar-value neutrality holds for any fairly
priced single swap independent of loop closure. The rate must be threaded
through explicitly (asset_usd_price /= rate each hop) for a loop
inconsistency to show up as more or fewer physical units of the base
asset at the end, which is what an arbitrage profit actually is.
"""

import time
import uuid
from dataclasses import dataclass, field

from app.config.constants import Strategy
from app.onchain.constants import (
    DEX_CAPITAL_TEST_TIERS_USD,
    MIN_NET_EDGE_PCT,
    NOMINAL_DEX_HOLDING_SECONDS,
    SLIPPAGE_BUFFER_PCT,
)
from app.onchain.cross_dex_arbitrage import estimate_amm_output_usd
from app.onchain.execution_model import build_execution_model
from app.onchain.mev_risk import compute_mev_risk_score, mev_buffer_pct_for_risk
from app.onchain.models import DexPool
from app.opportunity.models import Opportunity

# Default cycle length — a triangular loop (3 swaps). A 4th hop is
# supported by build_token_graph/find_cycles but never used by default
# (spec section 6: "Allow 4 only if testing proves useful").
MAX_ROUTE_LENGTH = 3


@dataclass(slots=True)
class RouteHop:
    pool: DexPool
    from_token: str
    to_token: str


@dataclass(slots=True)
class RouteResult:
    hops: list[RouteHop]
    input_usd: float
    output_usd: float
    net_profit_usd: float
    net_return_pct: float


def _price_of_from_to(pool: DexPool, from_token: str, to_token: str) -> float | None:
    """Real price of from_token in to_token units on this specific pool —
    NOT an impact-adjusted figure, the pool's own quoted rate."""
    if pool.token0_symbol.upper() == from_token.upper() and pool.token1_symbol.upper() == to_token.upper():
        return pool.price
    if pool.token0_symbol.upper() == to_token.upper() and pool.token1_symbol.upper() == from_token.upper():
        return (1 / pool.price) if pool.price else None
    return None


def build_token_graph(pools: list[DexPool]) -> dict[str, list[tuple[str, DexPool]]]:
    """token -> [(neighbor_token, pool_that_connects_them), ...] — each
    pool contributes an edge in both directions (a swap can go either way)."""
    graph: dict[str, list[tuple[str, DexPool]]] = {}
    for pool in pools:
        t0, t1 = pool.token0_symbol.upper(), pool.token1_symbol.upper()
        graph.setdefault(t0, []).append((t1, pool))
        graph.setdefault(t1, []).append((t0, pool))
    return graph


def find_cycles(graph: dict[str, list[tuple[str, DexPool]]], base_asset: str, max_hops: int = MAX_ROUTE_LENGTH) -> list[list[RouteHop]]:
    """DFS from base_asset back to base_asset, 2..max_hops swaps, never
    revisiting a token mid-path (prunes the trivial A->B->A back-and-forth
    and any longer redundant loop) or reusing the same pool twice in one
    path (an immediate round-trip on one pool is never a real 2-hop cycle,
    it's just paying that pool's fee twice for nothing)."""
    base_asset = base_asset.upper()
    cycles: list[list[RouteHop]] = []

    def dfs(current: str, path: list[RouteHop], visited_tokens: set[str], visited_pools: set[str]) -> None:
        if len(path) >= 2 and current == base_asset:
            cycles.append(list(path))
        if len(path) >= max_hops:
            return
        for neighbor, pool in graph.get(current, []):
            if pool.pool_id in visited_pools:
                continue
            if neighbor == base_asset:
                dfs(neighbor, path + [RouteHop(pool, current, neighbor)], visited_tokens, visited_pools | {pool.pool_id})
                continue
            if neighbor in visited_tokens:
                continue
            dfs(neighbor, path + [RouteHop(pool, current, neighbor)], visited_tokens | {neighbor}, visited_pools | {pool.pool_id})

    dfs(base_asset, [], {base_asset}, set())
    return cycles


def simulate_route(
    hops: list[RouteHop],
    input_usd: float,
    gas_cost_usd: float,
    mev_risk_chain: str,
    min_pool_tvl_usd_in_route: float,
) -> RouteResult | None:
    """Walks the loop once, mirroring app.engines.triangular's own
    quantity + running-asset-price approach (see module docstring for why
    this, not a pure USD-value pass-through, is required)."""
    quantity = input_usd  # units of the base asset, treated as ~$1/unit (a stablecoin — same assumption CEX triangular already makes)
    asset_usd_price = 1.0

    for hop in hops:
        rate = _price_of_from_to(hop.pool, hop.from_token, hop.to_token)
        if rate is None or rate <= 0:
            return None
        notional_usd = quantity * asset_usd_price
        effective_notional_usd = estimate_amm_output_usd(notional_usd, hop.pool.tvl_usd)
        if effective_notional_usd <= 0:
            return None
        fee = effective_notional_usd * (hop.pool.fee_pct / 100)
        received_usd_value = effective_notional_usd - fee
        asset_usd_price = asset_usd_price / rate
        if asset_usd_price <= 0:
            return None
        quantity = received_usd_value / asset_usd_price

    output_usd = quantity  # back in base-asset units, ~$1/unit
    gross_profit_usd = output_usd - input_usd

    mev_risk_score = compute_mev_risk_score(mev_risk_chain, input_usd, min_pool_tvl_usd_in_route)
    slippage_cost = input_usd * (SLIPPAGE_BUFFER_PCT / 100)
    mev_cost = input_usd * (mev_buffer_pct_for_risk(mev_risk_score) / 100)
    net_profit_usd = gross_profit_usd - gas_cost_usd - slippage_cost - mev_cost
    net_return_pct = (net_profit_usd / input_usd * 100) if input_usd else 0.0

    return RouteResult(hops=hops, input_usd=input_usd, output_usd=output_usd, net_profit_usd=net_profit_usd, net_return_pct=net_return_pct)


@dataclass(slots=True)
class RouteDepthAdjustedEdge:
    """Smart Position Sizing for a multi-hop route (spec section 16) — same
    shape as app.onchain.cross_dex_arbitrage.DexDepthAdjustedEdge: walk a
    spread of capital tiers, find the size that maximizes real dollar
    profit (not the best %), and the size where profit crosses back to
    zero. This was the piece flagged incomplete in the prior V5.5 report
    (multi-hop used a single fixed size); completed here rather than left
    as a known gap."""

    tiers: list[RouteResult]
    optimal_result: RouteResult | None
    max_profitable_capital_usd: float | None


def _interpolate_route_zero_crossing(last_profitable: RouteResult, first_unprofitable: RouteResult) -> float:
    profit_delta = first_unprofitable.net_profit_usd - last_profitable.net_profit_usd
    if profit_delta == 0:
        return last_profitable.input_usd
    capital_delta = first_unprofitable.input_usd - last_profitable.input_usd
    fraction = -last_profitable.net_profit_usd / profit_delta
    return last_profitable.input_usd + fraction * capital_delta


def compute_route_depth_adjusted_edge(
    hops: list[RouteHop], gas_cost_usd: float, chain: str, min_pool_tvl_usd_in_route: float, test_tiers_usd: list[float] = DEX_CAPITAL_TEST_TIERS_USD
) -> RouteDepthAdjustedEdge:
    tiers = [r for size in test_tiers_usd if (r := simulate_route(hops, size, gas_cost_usd, chain, min_pool_tvl_usd_in_route)) is not None]

    profitable_tiers = [t for t in tiers if t.net_profit_usd > 0]
    optimal = max(profitable_tiers, key=lambda t: t.net_profit_usd, default=None)

    max_profitable_capital_usd: float | None = None
    if optimal is not None:
        larger_tiers = sorted((t for t in tiers if t.input_usd > optimal.input_usd), key=lambda t: t.input_usd)
        first_unprofitable = next((t for t in larger_tiers if t.net_profit_usd <= 0), None)
        if first_unprofitable is not None:
            max_profitable_capital_usd = _interpolate_route_zero_crossing(optimal, first_unprofitable)
        else:
            max_profitable_capital_usd = larger_tiers[-1].input_usd if larger_tiers else optimal.input_usd

    return RouteDepthAdjustedEdge(tiers=tiers, optimal_result=optimal, max_profitable_capital_usd=max_profitable_capital_usd)


def detect_multihop_opportunity(
    graph: dict[str, list[tuple[str, DexPool]]],
    base_asset: str,
    input_usd: float,
    gas_cost_usd_per_hop: float,
    chain: str,
    max_hops: int = MAX_ROUTE_LENGTH,
) -> Opportunity | None:
    """Searches every cycle up to max_hops from base_asset. For each cycle,
    runs the full Smart Position Sizing tiered sweep (spec section 16) to
    find ITS OWN optimal size — a cycle that's mediocre at the naive
    `input_usd` default can still be the best real opportunity at a
    different size, so cycles are compared by their own best result, not
    all priced at one fixed size. Rejects if nothing clears MIN_NET_EDGE_PCT
    at any tested size for any cycle — same "not a real opportunity
    otherwise" rejection semantics as cross_dex_arbitrage. Rejects outright
    (before pricing) on Block Latency (spec section 14) — a multi-hop
    transaction needs the WHOLE chain of swaps included together, so it's
    at least as latency-sensitive as a single cross-DEX swap, never less.
    """
    if not build_execution_model(chain).is_capturable():
        return None

    cycles = find_cycles(graph, base_asset, max_hops=max_hops)
    best: RouteResult | None = None
    best_max_profitable_capital_usd: float | None = None
    for hops in cycles:
        min_pool_tvl = min(hop.pool.tvl_usd for hop in hops)
        gas_cost_usd = gas_cost_usd_per_hop * len(hops)
        edge = compute_route_depth_adjusted_edge(hops, gas_cost_usd, chain, min_pool_tvl)
        if edge.optimal_result is None:
            continue
        if best is None or edge.optimal_result.net_profit_usd > best.net_profit_usd:
            best = edge.optimal_result
            best_max_profitable_capital_usd = edge.max_profitable_capital_usd

    if best is None or best.net_return_pct < MIN_NET_EDGE_PCT:
        return None

    strategy = Strategy.DEX_TRIANGULAR if len(best.hops) == 3 else Strategy.DEX_MULTIHOP
    route_symbol = "->".join([base_asset] + [hop.to_token for hop in best.hops])
    # price/tvl_usd/fee_pct snapshotted here — Replay/Audit (user directive,
    # 2026-08-22): "no black box" means a historical opportunity must be
    # re-derivable from what was actually observed, not just its
    # already-computed output numbers.
    legs = [
        {
            "chain": chain, "exchange": hop.pool.dex, "side": "swap", "market": "dex", "pool_id": hop.pool.pool_id,
            "from": hop.from_token, "to": hop.to_token,
            "price": _price_of_from_to(hop.pool, hop.from_token, hop.to_token), "tvl_usd": hop.pool.tvl_usd, "fee_pct": hop.pool.fee_pct,
        }
        for hop in best.hops
    ]

    return Opportunity(
        strategy=strategy,
        symbol=route_symbol,
        legs=legs,
        gross_spread_pct=(best.output_usd - best.input_usd) / best.input_usd * 100 if best.input_usd else 0.0,
        net_spread_pct=best.net_return_pct,
        capital_usd=best.input_usd,
        expected_profit_usd=best.net_profit_usd,
        execution_fill_probability=max(0.5, 0.85 ** len(best.hops)),  # each additional hop compounds fill/inclusion risk — conservative, documented
        market_data_age_seconds=max(0.0, time.time() - max(hop.pool.last_update for hop in best.hops)),
        holding_period_seconds=NOMINAL_DEX_HOLDING_SECONDS,
        theoretical_edge_pct=(best.output_usd - best.input_usd) / best.input_usd * 100 if best.input_usd else 0.0,
        depth_adjusted_edge_pct=best.net_return_pct,
        realistic_executable_edge_pct=best.net_return_pct,
        optimal_capital_usd=best.input_usd,
        max_profitable_capital_usd=best_max_profitable_capital_usd,
        capital_is_liquidity_capped=True,
        detected_at=time.time(),
        id=uuid.uuid4(),
    )
