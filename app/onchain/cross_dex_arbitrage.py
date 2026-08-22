"""Cross-DEX Arbitrage (Multi-Market Opportunity Engine, V5.5, spec section 5).

Compares the same asset pair across 2+ DEXs on the SAME chain (see
app.onchain.constants.DexVenue's own docstring for why eth/bsc aren't
compared against each other — a real cross-chain bridge takes minutes, not
seconds, which the Fast Trading Rule doesn't allow) and prices a realistic
net result: gross spread, minus swap fees, minus gas, minus price impact,
minus a slippage buffer, minus an MEV buffer. Only ever reports an
opportunity as executable if that final net edge clears a configured
minimum — never as a shortcut to more opportunities (spec section 21).

Price impact is modeled with the constant-product (x*y=k) AMM formula
against the one number a DEX aggregator API actually exposes (aggregate
pool TVL), assuming a 50/50 USD-value split between the two reserves —
exact for a standard Uniswap V2/Raydium-style pool, a first-order
approximation for concentrated liquidity (Uniswap V3/Orca Whirlpools),
where the true curve depends on the active tick range this data doesn't
expose. Documented as an approximation, not fabricated precision.

Smart Position Sizing (spec section 16) — same shape as
app.analytics.execution_depth's CEX depth-adjusted curve: walk a spread of
capital tiers, find the size that maximizes real dollar profit (not the
best %), and the size where profit crosses back to zero. Kept as its own,
separate implementation rather than sharing code with the CEX version —
different underlying math (AMM curve vs. order-book VWAP) and deliberate
isolation from the CEX engine (spec section 1).

Also applies, per opportunity: Block Latency (spec section 14) — rejects
outright if the chain's own expected transaction-inclusion time exceeds
that chain's documented expected opportunity lifetime (app.onchain.execution_model) —
and MEV Risk (spec section 13) — scales the flat MEV buffer up for
trades that are large relative to the pool they trade against
(app.onchain.mev_risk), rather than one flat assumption for every size.
"""

import time
import uuid
from dataclasses import dataclass

from app.config.constants import Strategy
from app.onchain.constants import (
    DEX_CAPITAL_TEST_TIERS_USD,
    DEX_EXECUTION_FILL_PROBABILITY,
    MIN_NET_EDGE_PCT,
    NOMINAL_DEX_HOLDING_SECONDS,
    SLIPPAGE_BUFFER_PCT,
)
from app.onchain.execution_model import build_execution_model
from app.onchain.mev_risk import compute_mev_risk_score, mev_buffer_pct_for_risk
from app.onchain.models import DexPool
from app.opportunity.models import Opportunity


def estimate_amm_output_usd(trade_size_usd: float, pool_tvl_usd: float) -> float:
    """Constant-product (x*y=k) price-impact approximation — see module
    docstring for the 50/50-split assumption this rests on. Returns the USD
    value received for trade_size_usd traded in, before fees (fees are
    applied separately by the caller)."""
    if pool_tvl_usd <= 0 or trade_size_usd <= 0:
        return 0.0
    reserve_side_usd = pool_tvl_usd / 2
    return (reserve_side_usd * trade_size_usd) / (reserve_side_usd + trade_size_usd)


@dataclass(slots=True)
class DexTierResult:
    capital_usd: float
    net_profit_usd: float
    net_pct: float


def evaluate_dex_capital_tier(
    buy_pool: DexPool, sell_pool: DexPool, buy_price: float, sell_price: float, capital_usd: float, gas_cost_usd: float
) -> DexTierResult:
    """Walks both legs' AMM curves at one capital size — the DEX analogue
    of app.analytics.execution_depth.evaluate_capital_tier.

    buy_price/sell_price are asset_a's price in asset_b on each pool
    (app.onchain.cross_dex_arbitrage._price_of_a_in_b's normalized value) —
    without threading these through explicitly, estimate_amm_output_usd's
    pure USD-value pass-through has no way to know the two pools quote
    asset_a at different rates in the first place, which is the ENTIRE
    source of the arbitrage: a bug caught live testing this exact function
    with a real 1%-apart-priced pair, where every tested size came back a
    loss despite a genuine cross-pool price gap, because the calculation
    silently never multiplied by the price difference at all.
    """
    # Leg 1: spend capital_usd of asset_b on buy_pool to acquire asset_a —
    # filled_usd_leg1 is "value of asset_a acquired, valued at buy_pool's
    # OWN rate" (that's what a same-pool constant-product swap means); the
    # physical quantity acquired is that value divided by buy_pool's price.
    filled_usd_leg1 = estimate_amm_output_usd(capital_usd, buy_pool.tvl_usd)
    buy_fee = filled_usd_leg1 * (buy_pool.fee_pct / 100)
    quantity_asset_a = (filled_usd_leg1 - buy_fee) / buy_price if buy_price else 0.0

    # Leg 2: that quantity of asset_a is worth quantity * sell_price on
    # sell_pool BEFORE that pool's own impact — this is the step that
    # actually captures the cross-pool price difference, since sell_price
    # != buy_price is exactly the arbitrage.
    notional_at_sell_pool_price = quantity_asset_a * sell_price
    filled_usd_leg2 = estimate_amm_output_usd(notional_at_sell_pool_price, sell_pool.tvl_usd)
    sell_fee = filled_usd_leg2 * (sell_pool.fee_pct / 100)
    final_usd = filled_usd_leg2 - sell_fee

    slippage_cost = capital_usd * (SLIPPAGE_BUFFER_PCT / 100)
    # MEV Risk (spec section 13) — scaled by how large this trade is
    # relative to the THINNER of the two pools (the more conservative,
    # higher-risk choice), not a flat assumption regardless of size.
    mev_risk_score = compute_mev_risk_score(buy_pool.chain, capital_usd, min(buy_pool.tvl_usd, sell_pool.tvl_usd))
    mev_cost = capital_usd * (mev_buffer_pct_for_risk(mev_risk_score) / 100)
    net_profit = final_usd - capital_usd - gas_cost_usd - slippage_cost - mev_cost
    net_pct = (net_profit / capital_usd * 100) if capital_usd else 0.0
    return DexTierResult(capital_usd=capital_usd, net_profit_usd=net_profit, net_pct=net_pct)


@dataclass(slots=True)
class DexDepthAdjustedEdge:
    theoretical_edge_pct: float  # raw price spread between the two venues, before any cost
    tiers: list[DexTierResult]
    optimal_capital_usd: float | None
    optimal_net_profit_usd: float | None
    optimal_net_pct: float | None
    max_profitable_capital_usd: float | None


def _interpolate_zero_crossing(last_profitable: DexTierResult, first_unprofitable: DexTierResult) -> float:
    profit_delta = first_unprofitable.net_profit_usd - last_profitable.net_profit_usd
    if profit_delta == 0:
        return last_profitable.capital_usd
    capital_delta = first_unprofitable.capital_usd - last_profitable.capital_usd
    fraction = -last_profitable.net_profit_usd / profit_delta
    return last_profitable.capital_usd + fraction * capital_delta


def compute_dex_depth_adjusted_edge(
    buy_pool: DexPool,
    sell_pool: DexPool,
    buy_price: float,
    sell_price: float,
    gas_cost_usd: float,
    theoretical_edge_pct: float,
    test_tiers_usd: list[float] = DEX_CAPITAL_TEST_TIERS_USD,
) -> DexDepthAdjustedEdge:
    tiers = [evaluate_dex_capital_tier(buy_pool, sell_pool, buy_price, sell_price, size, gas_cost_usd) for size in test_tiers_usd]

    profitable_tiers = [t for t in tiers if t.net_profit_usd > 0]
    optimal = max(profitable_tiers, key=lambda t: t.net_profit_usd, default=None)

    max_profitable_capital_usd: float | None = None
    if optimal is not None:
        larger_tiers = sorted((t for t in tiers if t.capital_usd > optimal.capital_usd), key=lambda t: t.capital_usd)
        first_unprofitable = next((t for t in larger_tiers if t.net_profit_usd <= 0), None)
        if first_unprofitable is not None:
            max_profitable_capital_usd = _interpolate_zero_crossing(optimal, first_unprofitable)
        else:
            max_profitable_capital_usd = larger_tiers[-1].capital_usd if larger_tiers else optimal.capital_usd

    return DexDepthAdjustedEdge(
        theoretical_edge_pct=theoretical_edge_pct,
        tiers=tiers,
        optimal_capital_usd=optimal.capital_usd if optimal else None,
        optimal_net_profit_usd=optimal.net_profit_usd if optimal else None,
        optimal_net_pct=optimal.net_pct if optimal else None,
        max_profitable_capital_usd=max_profitable_capital_usd,
    )


def _price_of_a_in_b(pool: DexPool, asset_a: str, asset_b: str) -> float | None:
    """Normalizes a pool's own base/quote price into "how much of asset_b
    per 1 unit of asset_a", regardless of which token GeckoTerminal called
    token0 vs token1 for this specific pool — two pools for the same
    conceptual pair can list them in either order."""
    if pool.token0_symbol.upper() == asset_a.upper() and pool.token1_symbol.upper() == asset_b.upper():
        return pool.price
    if pool.token0_symbol.upper() == asset_b.upper() and pool.token1_symbol.upper() == asset_a.upper():
        return (1 / pool.price) if pool.price else None
    return None


def order_buy_sell_pools(pool_a: DexPool, pool_b: DexPool) -> tuple[DexPool, DexPool, float, float, float] | None:
    """Determines which of two same-chain, same-pair pools is cheaper (buy
    there) and which is richer (sell there), and the resulting theoretical
    (top-of-book, cost-blind) edge. Returns None if they're not actually
    the same pair or there's no real price difference. Factored out of
    detect_cross_dex_opportunity so app.onchain.flash_loan_research can
    reuse the exact same ordering/pricing logic rather than re-deriving it
    (and risking the two silently drifting apart)."""
    if pool_a.chain != pool_b.chain:
        return None
    asset_a, asset_b = sorted([pool_a.token0_symbol.upper(), pool_a.token1_symbol.upper()])
    price_a_on_pool_a = _price_of_a_in_b(pool_a, asset_a, asset_b)
    price_a_on_pool_b = _price_of_a_in_b(pool_b, asset_a, asset_b)
    if price_a_on_pool_a is None or price_a_on_pool_b is None or price_a_on_pool_a <= 0 or price_a_on_pool_b <= 0:
        return None

    if price_a_on_pool_a < price_a_on_pool_b:
        theoretical_edge_pct = (price_a_on_pool_b - price_a_on_pool_a) / price_a_on_pool_a * 100
        if theoretical_edge_pct <= 0:
            return None
        return pool_a, pool_b, price_a_on_pool_a, price_a_on_pool_b, theoretical_edge_pct
    if price_a_on_pool_b < price_a_on_pool_a:
        theoretical_edge_pct = (price_a_on_pool_a - price_a_on_pool_b) / price_a_on_pool_b * 100
        if theoretical_edge_pct <= 0:
            return None
        return pool_b, pool_a, price_a_on_pool_b, price_a_on_pool_a, theoretical_edge_pct
    return None


def detect_cross_dex_opportunity(
    pool_a: DexPool, pool_b: DexPool, gas_cost_usd_a: float, gas_cost_usd_b: float
) -> Opportunity | None:
    """Compares two same-chain pools for the same asset pair. Returns None
    if they're not actually the same pair, if the raw spread isn't even
    positive, or if depth-adjusted sizing never clears MIN_NET_EDGE_PCT at
    any tested size — same "not a real opportunity" rejection semantics
    app.engines._shared uses for CEX."""
    ordered = order_buy_sell_pools(pool_a, pool_b)
    if ordered is None:
        return None
    buy_pool, sell_pool, buy_price, sell_price, theoretical_edge_pct = ordered
    buy_gas, sell_gas = (gas_cost_usd_a, gas_cost_usd_b) if buy_pool is pool_a else (gas_cost_usd_b, gas_cost_usd_a)
    asset_a, asset_b = sorted([pool_a.token0_symbol.upper(), pool_a.token1_symbol.upper()])

    # Block Latency (spec section 14) — "A DEX opportunity that exists for
    # 100ms may be impossible to capture on-chain." Rejected here, before
    # any cost math, since a non-capturable opportunity isn't worth pricing
    # at all: no execution model can promise the price gap survives long
    # enough for the transaction to land.
    if not build_execution_model(pool_a.chain).is_capturable():
        return None

    total_gas_usd = buy_gas + sell_gas
    edge = compute_dex_depth_adjusted_edge(buy_pool, sell_pool, buy_price, sell_price, total_gas_usd, theoretical_edge_pct)
    if edge.optimal_capital_usd is None or edge.optimal_net_pct is None or edge.optimal_net_pct < MIN_NET_EDGE_PCT:
        return None

    return Opportunity(
        strategy=Strategy.DEX_CROSS,
        symbol=f"{asset_a}/{asset_b}",
        legs=[
            {"chain": buy_pool.chain, "exchange": buy_pool.dex, "side": "buy", "market": "dex", "pool_id": buy_pool.pool_id},
            {"chain": sell_pool.chain, "exchange": sell_pool.dex, "side": "sell", "market": "dex", "pool_id": sell_pool.pool_id},
        ],
        gross_spread_pct=theoretical_edge_pct,
        net_spread_pct=edge.optimal_net_pct,
        capital_usd=edge.optimal_capital_usd,
        expected_profit_usd=edge.optimal_net_profit_usd,
        execution_fill_probability=DEX_EXECUTION_FILL_PROBABILITY,
        market_data_age_seconds=max(0.0, time.time() - max(buy_pool.last_update, sell_pool.last_update)),
        holding_period_seconds=NOMINAL_DEX_HOLDING_SECONDS,
        theoretical_edge_pct=theoretical_edge_pct,
        depth_adjusted_edge_pct=edge.optimal_net_pct,
        realistic_executable_edge_pct=edge.optimal_net_pct,
        optimal_capital_usd=edge.optimal_capital_usd,
        max_profitable_capital_usd=edge.max_profitable_capital_usd,
        capital_is_liquidity_capped=True,
        detected_at=time.time(),
        id=uuid.uuid4(),
    )
