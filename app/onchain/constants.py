"""On-chain configuration (Multi-Market Opportunity Engine, V5.5, user
directive, 2026-08-21). Kept separate from app.config.constants — the CEX
engine must never need to know these exist, and the DEX side must never
need to know CEX's (isolation, spec section 1)."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class DexVenue:
    chain: str  # GeckoTerminal network id — "eth", "bsc", "solana"
    dex: str  # GeckoTerminal dex id within that network


# Section 3's starting ecosystems. Uniswap and PancakeSwap are on different
# chains (Ethereum vs BSC) — a "cross-DEX" comparison between them would
# require an actual cross-chain bridge, which takes minutes, not seconds,
# violating the Fast Trading Rule outright (spec section 2). Raydium and
# Orca are both native to Solana, so they're the pair this first pass
# actually runs cross-DEX arbitrage DETECTION on — same-chain, genuinely
# fast-executable. Uniswap/PancakeSwap pools are still discovered and
# monitored live (acceptance criterion 2) even though cross-DEX detection
# isn't wired for them yet; adding a second same-chain EVM DEX (e.g.
# Sushiswap on Ethereum) is the natural next step, not done tonight.
DEX_VENUES: list[DexVenue] = [
    DexVenue(chain="eth", dex="uniswap_v3"),
    DexVenue(chain="bsc", dex="pancakeswap-v3-bsc"),
    DexVenue(chain="solana", dex="raydium"),
    DexVenue(chain="solana", dex="orca"),
]

# Same-chain venue groups cross-DEX arbitrage actually compares pools
# across — see the DexVenue docstring above for why eth/bsc aren't grouped
# together.
CROSS_DEX_CHAINS: list[str] = ["solana"]

# Section 4's own list, "prioritize liquid assets such as..." — a token
# must appear on BOTH sides of a pool for that pool to be discovered at
# all (stable/major-to-stable/major pairs only, no arbitrary long-tail
# token chasing merely because a spread looks large — spec section 31).
WHITELISTED_SYMBOLS = {"USDT", "USDC", "FDUSD", "ETH", "WETH", "BTC", "WBTC", "BNB", "WBNB", "SOL", "WSOL"}

# A pool below this 24h-reserve USD value is real but too thin to treat as
# a genuine fast-rotation candidate — same reasoning as
# app.market_data.symbol_discovery's MIN_QUOTE_VOLUME_USD for CEX symbols,
# and also filters out "dead pools" (spec section 4).
MIN_POOL_TVL_USD = 100_000.0

# A pool younger than this is more likely to be a fresh/unverified listing
# than a genuinely liquid, stable market (spec section 4's "unsafe/unverified
# assets", section 31's token-safety filter, first pass).
MIN_POOL_AGE_HOURS = 24.0

# Conservative, explicitly-documented placeholder assumptions (spec
# sections 5, 13) — not measurements. Refined once real shadow-execution
# outcomes exist to calibrate against, same caveat as
# app.config.constants.DEFAULT_SLIPPAGE_BUFFER_PCT for CEX.
SLIPPAGE_BUFFER_PCT = 0.05
MEV_BUFFER_PCT = 0.05
# A DEX swap's true fill probability isn't 100% the way a CEX taker order
# is — front-running, sandwiching, and the price moving before block
# inclusion can all still cause the realized result to miss the quoted one
# even when a transaction lands. Conservative, not a measurement.
DEX_EXECUTION_FILL_PROBABILITY = 0.85

# Position-sizing tiers for the "smart position sizing" curve (spec
# section 16) — same shape as the CEX Depth-Adjusted Execution Curve
# (app.analytics.execution_depth), applied against DEX pool depth instead
# of a CEX order book.
DEX_CAPITAL_TEST_TIERS_USD = [10, 25, 50, 100, 250, 500, 1_000, 5_000, 10_000]

# Minimum net edge, after every realistic cost, for a DEX opportunity to be
# reported as executable at all — never lowered to manufacture more
# opportunities (spec section 21).
MIN_NET_EDGE_PCT = 0.05

# Section 2's global Fast Trading Rule, applied to DEX too — a shadow
# cross-DEX swap is modeled as a near-instant round trip once broadcast
# (well under the "seconds -> 6 minutes" preferred band), same
# NOMINAL_FAST_HOLDING_SECONDS-style nominal used for CEX cross-exchange.
NOMINAL_DEX_HOLDING_SECONDS = 20.0

# GeckoTerminal's public (no API key) tier — respect it; a burst of
# requests returns 429s, confirmed live while researching this feature.
GECKOTERMINAL_MIN_REQUEST_INTERVAL_SECONDS = 2.5

# Multi-Hop / DEX Triangular Arbitrage (spec sections 6, 7) — the base
# asset a cycle starts and ends at must be a stablecoin, matching CEX
# triangular's own convention (app.engines.triangular's TRIANGULAR_PATHS
# all start from "USDT"). Search runs once per one of these actually
# present in a given chain's discovered pools, not a single hardcoded one.
DEX_STABLECOIN_BASE_ASSETS = {"USDC", "USDT", "FDUSD"}

# A single representative trade size for multi-hop detection (spec section
# 16's own tiered sweep is applied to cross-DEX opportunities via
# compute_dex_depth_adjusted_edge; extending that same tiered sweep across
# N-hop routes is a larger follow-up — see the V5.5 completion report —
# not done for multi-hop yet, so this fixed size stands in for it, same
# role app.config.constants.DEFAULT_OPPORTUNITY_CAPITAL_USD plays for CEX).
DEFAULT_DEX_TRADE_SIZE_USD = 1_000.0
