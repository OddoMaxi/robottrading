import pytest

from app.config.constants import Strategy
from app.onchain.models import DexPool
from app.onchain.multihop_arbitrage import (
    RouteHop,
    build_token_graph,
    detect_multihop_opportunity,
    find_cycles,
    simulate_route,
)


def _pool(dex, t0, t1, price, tvl=50_000_000.0, fee=0.05, chain="eth", pid=None) -> DexPool:
    return DexPool(
        chain=chain, dex=dex, pool_id=pid or f"{chain}_{dex}_{t0}{t1}", token0_symbol=t0, token1_symbol=t1,
        price=price, tvl_usd=tvl, volume_24h_usd=1_000_000.0, fee_pct=fee, pool_created_at=None, last_update=1_800_000_000.0,
    )


def _triangle():
    p1 = _pool("uniswap_v3", "ETH", "USDC", 2500.0)
    p2 = _pool("uniswap_v3", "ETH", "SOL", 27.0)  # richer than fair (2500/95 = 26.3158) — the mispricing
    p3 = _pool("uniswap_v3", "SOL", "USDC", 95.0)
    return p1, p2, p3


def test_build_token_graph_creates_bidirectional_edges():
    p1, p2, p3 = _triangle()
    graph = build_token_graph([p1, p2, p3])
    assert {n for n, _ in graph["USDC"]} == {"ETH", "SOL"}
    assert {n for n, _ in graph["ETH"]} == {"USDC", "SOL"}


def test_find_cycles_finds_both_directions_of_the_same_triangle():
    p1, p2, p3 = _triangle()
    graph = build_token_graph([p1, p2, p3])
    cycles = find_cycles(graph, "USDC", max_hops=3)
    paths = {"->".join(["USDC"] + [h.to_token for h in c]) for c in cycles}
    assert "USDC->ETH->SOL->USDC" in paths
    assert "USDC->SOL->ETH->USDC" in paths
    assert len(cycles) == 2


def test_find_cycles_never_revisits_a_token_mid_path():
    p1, p2, p3 = _triangle()
    graph = build_token_graph([p1, p2, p3])
    cycles = find_cycles(graph, "USDC", max_hops=3)
    for cycle in cycles:
        intermediate_tokens = [h.to_token for h in cycle[:-1]]
        assert len(intermediate_tokens) == len(set(intermediate_tokens))


def test_find_cycles_respects_max_hops():
    p1, p2, p3 = _triangle()
    graph = build_token_graph([p1, p2, p3])
    assert find_cycles(graph, "USDC", max_hops=2) == []  # this triangle needs exactly 3 hops


def test_simulate_route_forward_direction_captures_the_real_mispricing():
    p1, p2, p3 = _triangle()
    hops = [RouteHop(p1, "USDC", "ETH"), RouteHop(p2, "ETH", "SOL"), RouteHop(p3, "SOL", "USDC")]
    result = simulate_route(hops, 1000.0, gas_cost_usd=0.5, mev_risk_chain="eth", min_pool_tvl_usd_in_route=50_000_000.0)
    assert result is not None
    assert result.net_profit_usd == pytest.approx(22.47891015262293, rel=1e-6)
    assert result.net_return_pct == pytest.approx(2.247891015262293, rel=1e-6)


def test_simulate_route_reverse_direction_of_the_same_mispricing_is_a_loss():
    p1, p2, p3 = _triangle()
    hops = [RouteHop(p3, "USDC", "SOL"), RouteHop(p2, "SOL", "ETH"), RouteHop(p1, "ETH", "USDC")]
    result = simulate_route(hops, 1000.0, gas_cost_usd=0.5, mev_risk_chain="eth", min_pool_tvl_usd_in_route=50_000_000.0)
    assert result is not None
    assert result.net_profit_usd < 0


def test_simulate_route_internally_consistent_loop_nets_to_near_zero_before_buffers():
    """Regression for a real bug caught building this feature: chaining
    pure USD-value-pass-through (no explicit rate tracking) at every hop is
    dollar-value-invariant BY CONSTRUCTION, so it can never detect a
    genuine triangular mispricing — every loop looks like ~0 profit minus
    fees regardless of whether the real rates are consistent or not. This
    proves the fix: with a loop whose real rates DO multiply consistently
    (no genuine arbitrage) and zero fees/gas, output must be ~= input
    (only a few cents of AMM-impact rounding on $1000 against $50M pools),
    not systematically zero for every loop shape."""
    fair_eth_sol = 2500.0 / 95.0
    p1 = _pool("uniswap_v3", "ETH", "USDC", 2500.0, fee=0.0)
    p2 = _pool("uniswap_v3", "ETH", "SOL", fair_eth_sol, fee=0.0)
    p3 = _pool("uniswap_v3", "SOL", "USDC", 95.0, fee=0.0)
    hops = [RouteHop(p1, "USDC", "ETH"), RouteHop(p2, "ETH", "SOL"), RouteHop(p3, "SOL", "USDC")]
    result = simulate_route(hops, 1000.0, gas_cost_usd=0.0, mev_risk_chain="eth", min_pool_tvl_usd_in_route=50_000_000.0)
    assert result is not None
    assert result.output_usd == pytest.approx(1000.0, abs=0.5)  # only tiny AMM-impact rounding, not a fee/gas cost (both zero here)


def test_simulate_route_invalid_hop_price_returns_none():
    p1, _p2, _p3 = _triangle()
    bad_pool = _pool("uniswap_v3", "USDC", "ETH", 0.0)  # zero price is invalid
    hops = [RouteHop(bad_pool, "USDC", "ETH")]
    assert simulate_route(hops, 1000.0, 0.0, "eth", 1_000_000.0) is None


def test_detect_multihop_opportunity_selects_the_profitable_direction_and_tags_triangular():
    p1, p2, p3 = _triangle()
    graph = build_token_graph([p1, p2, p3])
    opp = detect_multihop_opportunity(graph, "USDC", 1000.0, gas_cost_usd_per_hop=0.5, chain="eth")
    assert opp is not None
    assert opp.strategy == Strategy.DEX_TRIANGULAR
    assert opp.symbol == "USDC->ETH->SOL->USDC"
    assert opp.capital_usd == 1000.0
    assert opp.expected_profit_usd > 0


def test_detect_multihop_opportunity_no_cycle_found_returns_none():
    p1 = _pool("uniswap_v3", "USDC", "ETH", 0.0004)  # only one pool — no cycle possible
    graph = build_token_graph([p1])
    assert detect_multihop_opportunity(graph, "USDC", 1000.0, gas_cost_usd_per_hop=0.5, chain="eth") is None


def test_detect_multihop_opportunity_a_thin_edge_below_minimum_is_rejected():
    p1 = _pool("uniswap_v3", "ETH", "USDC", 2500.0)
    p2 = _pool("uniswap_v3", "ETH", "SOL", 2500.0 / 95.0 * 1.0002)  # only a hair off fair — won't clear costs
    p3 = _pool("uniswap_v3", "SOL", "USDC", 95.0)
    graph = build_token_graph([p1, p2, p3])
    assert detect_multihop_opportunity(graph, "USDC", 1000.0, gas_cost_usd_per_hop=0.5, chain="eth") is None


def test_detect_multihop_opportunity_a_non_capturable_chain_rejects_even_a_real_gap(monkeypatch):
    import app.onchain.multihop_arbitrage as module

    class _NeverCapturable:
        def is_capturable(self):
            return False

    monkeypatch.setattr(module, "build_execution_model", lambda chain: _NeverCapturable())
    p1, p2, p3 = _triangle()
    graph = build_token_graph([p1, p2, p3])
    assert detect_multihop_opportunity(graph, "USDC", 1000.0, gas_cost_usd_per_hop=0.5, chain="eth") is None


def test_four_hop_route_is_tagged_multihop_not_triangular():
    # USDC -> ETH -> SOL -> BNB -> USDC, a genuine 4-hop cycle.
    p1 = _pool("uniswap_v3", "ETH", "USDC", 2500.0, pid="p1")
    p2 = _pool("uniswap_v3", "ETH", "SOL", 30.0, pid="p2")  # deliberately generous rate to guarantee a clearable edge across 4 hops
    p3 = _pool("uniswap_v3", "SOL", "BNB", 0.15, pid="p3")
    p4 = _pool("uniswap_v3", "BNB", "USDC", 600.0, pid="p4")
    graph = build_token_graph([p1, p2, p3, p4])
    opp = detect_multihop_opportunity(graph, "USDC", 1000.0, gas_cost_usd_per_hop=0.5, chain="eth", max_hops=4)
    assert opp is not None
    assert opp.strategy == Strategy.DEX_MULTIHOP
    assert len(opp.legs) == 4
