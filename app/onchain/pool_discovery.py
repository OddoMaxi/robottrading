"""DEX Pool Discovery (Multi-Market Opportunity Engine, V5.5, spec section 4).

Filters raw pools from a DEXMarketDataProvider down to genuinely liquid,
whitelisted-asset, established markets — "prioritize liquid assets", "filter
out very low liquidity / unknown tokens / dead pools" are the spec's own
words for exactly this gate.
"""

from app.onchain.constants import MIN_POOL_AGE_HOURS, MIN_POOL_TVL_USD, WHITELISTED_SYMBOLS
from app.onchain.market_data_provider import DEXMarketDataProvider
from app.onchain.models import DexPool
from app.onchain.token_safety import is_token_safety_acceptable


def _is_eligible(pool: DexPool) -> bool:
    if pool.tvl_usd < MIN_POOL_TVL_USD:
        return False
    if pool.token0_symbol.upper() not in WHITELISTED_SYMBOLS or pool.token1_symbol.upper() not in WHITELISTED_SYMBOLS:
        return False
    age_hours = pool.age_hours
    if age_hours is not None and age_hours < MIN_POOL_AGE_HOURS:
        return False
    # extreme-spread pools (spec section 4) — a pool priced at literally
    # zero or an absurd outlier is a data artifact, not a real market.
    if pool.price <= 0:
        return False
    # Token Safety Filter (spec section 31) — the checks above are already
    # a hard whitelist gate; this additionally scores liquidity/age/activity
    # together rather than trusting any single one alone (a pool can clear
    # every individual floor above by a hair and still be a weak overall
    # market).
    if not is_token_safety_acceptable(pool):
        return False
    return True


def filter_eligible_pools(pools: list[DexPool]) -> list[DexPool]:
    """Pure filtering step — unit-testable without a network call, same
    pattern as app.market_data.symbol_discovery.build_discovered_universe."""
    return [p for p in pools if _is_eligible(p)]


async def discover_pools(provider: DEXMarketDataProvider, chain: str, dex: str, pages: int = 1) -> list[DexPool]:
    pools = await provider.fetch_pools(chain, dex, pages=pages)
    return filter_eligible_pools(pools)
