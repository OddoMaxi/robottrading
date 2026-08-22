"""Token Safety Filter (Multi-Market Opportunity Engine, V5.5, spec section 31).

"Never chase arbitrary unknown tokens merely because the spread is large."
app.onchain.pool_discovery's WHITELISTED_SYMBOLS is the hard gate (a token
must be a known major asset to be considered at all); this module adds the
"scoring system based on liquidity, token age, verification, market depth,
major listings, pool history" the spec also asks for — a transparent,
documented formula, not a claim of verified on-chain provenance. This does
NOT check contract source code, audit status, or holder distribution (a
real token-verification registry integration is future work, not
fabricated here) — it scores what the already-available pool data can
actually support: is this a major-listed asset, how liquid is the pool,
how long has it existed, how actively is it traded.
"""

from app.onchain.models import DexPool

# Both sides of a pool must be a known major asset to score any points here
# at all — same set app.onchain.constants.WHITELISTED_SYMBOLS already
# hard-gates on; scored here too so the "major listing" component of the
# formula is explicit rather than assumed.
MAJOR_ASSETS = {"USDT", "USDC", "FDUSD", "ETH", "WETH", "BTC", "WBTC", "BNB", "WBNB", "SOL", "WSOL"}

# Reference scales — a pool at or above these values scores full marks on
# that factor. Documented, conservative choices, not measurements.
LIQUIDITY_REFERENCE_USD = 1_000_000.0
AGE_REFERENCE_HOURS = 24.0 * 30  # 30 days
VOLUME_REFERENCE_USD = 500_000.0

MIN_TOKEN_SAFETY_SCORE = 0.6


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def compute_token_safety_score(pool: DexPool) -> float:
    """0-1, higher = safer. major_listing (both sides known majors) carries
    the heaviest weight since it's the closest thing to a hard safety
    guarantee this data can support; liquidity/age/activity refine it."""
    major_listing_score = 1.0 if (pool.token0_symbol.upper() in MAJOR_ASSETS and pool.token1_symbol.upper() in MAJOR_ASSETS) else 0.0
    liquidity_score = _clamp01(pool.tvl_usd / LIQUIDITY_REFERENCE_USD)
    age_hours = pool.age_hours
    # Unknown age (not every pool/DEX exposes pool_created_at) is scored
    # neutral, not penalized to zero nor rewarded to full marks — an
    # honest "no information" middle ground.
    age_score = _clamp01(age_hours / AGE_REFERENCE_HOURS) if age_hours is not None else 0.5
    activity_score = _clamp01(pool.volume_24h_usd / VOLUME_REFERENCE_USD)

    return round(0.40 * major_listing_score + 0.25 * liquidity_score + 0.15 * age_score + 0.20 * activity_score, 3)


def is_token_safety_acceptable(pool: DexPool, min_score: float = MIN_TOKEN_SAFETY_SCORE) -> bool:
    return compute_token_safety_score(pool) >= min_score
