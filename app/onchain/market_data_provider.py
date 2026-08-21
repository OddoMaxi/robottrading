"""DEX Market Data Provider (Multi-Market Opportunity Engine, V5.5, spec
section 3) — DEXMarketDataProvider is the adapter boundary so a future
second data source (a direct RPC-based provider, a different aggregator)
can be added without touching pool discovery, cross-DEX detection, or
anything downstream. GeckoTerminalProvider is the first, real
implementation: free, no API key, verified live (confirmed reachable and
correctly parsed for eth/uniswap_v3, bsc/pancakeswap-v3-bsc,
solana/raydium, solana/orca while building this).
"""

import asyncio
import logging
import re
import time
from abc import ABC, abstractmethod
from datetime import datetime

import aiohttp

from app.onchain.constants import GECKOTERMINAL_MIN_REQUEST_INTERVAL_SECONDS
from app.onchain.models import DexPool

logger = logging.getLogger(__name__)

# A pool name that follows Uniswap V3 / PancakeSwap V3's own convention
# ("USDC / WETH 0.01%") carries its real fee tier in the name itself — this
# extracts it. Pools without a trailing "N%" (Raydium/Orca standard pools)
# fall back to a documented per-DEX default below.
_FEE_TIER_PATTERN = re.compile(r"(\d+(?:\.\d+)?)\s*%\s*$")

# Not extractable from a pool's name the way Uniswap V3/PancakeSwap V3
# concentrated pools are — standard, documented protocol defaults.
_DEFAULT_FEE_PCT_BY_DEX = {
    "raydium": 0.25,
    "orca": 0.30,
}


class DEXMarketDataProvider(ABC):
    @abstractmethod
    async def fetch_pools(self, chain: str, dex: str, pages: int = 1) -> list[DexPool]: ...


def _parse_pool(chain: str, dex: str, row: dict, now: float) -> DexPool | None:
    attrs = row.get("attributes", {})
    name = attrs.get("name", "")

    # A Uniswap V3 / PancakeSwap V3 style name carries its fee tier as a
    # trailing "N%" (e.g. "USDC / WETH 0.01%") — extract it, then strip it
    # off before splitting on "/", or it would otherwise get glued onto
    # token1's symbol ("WETH 0.01%" instead of "WETH").
    fee_match = _FEE_TIER_PATTERN.search(name)
    fee_pct = float(fee_match.group(1)) if fee_match else _DEFAULT_FEE_PCT_BY_DEX.get(dex, 0.30)
    name_without_fee = _FEE_TIER_PATTERN.sub("", name).strip() if fee_match else name

    parts = [p.strip() for p in name_without_fee.split("/")]
    if len(parts) != 2:
        return None
    token0_symbol, token1_symbol = parts

    try:
        price = float(attrs["base_token_price_quote_token"])
        tvl_usd = float(attrs.get("reserve_in_usd") or 0)
        volume_24h_usd = float((attrs.get("volume_usd") or {}).get("h24") or 0)
    except (KeyError, TypeError, ValueError):
        return None

    created_at = None
    raw_created = attrs.get("pool_created_at")
    if raw_created:
        try:
            created_at = datetime.fromisoformat(raw_created.replace("Z", "+00:00"))
        except ValueError:
            created_at = None

    return DexPool(
        chain=chain,
        dex=dex,
        pool_id=row.get("id", ""),
        token0_symbol=token0_symbol,
        token1_symbol=token1_symbol,
        price=price,
        tvl_usd=tvl_usd,
        volume_24h_usd=volume_24h_usd,
        fee_pct=fee_pct,
        pool_created_at=created_at,
        last_update=now,
    )


class GeckoTerminalProvider(DEXMarketDataProvider):
    BASE_URL = "https://api.geckoterminal.com/api/v2"

    def __init__(self, min_request_interval_seconds: float = GECKOTERMINAL_MIN_REQUEST_INTERVAL_SECONDS) -> None:
        self._min_request_interval_seconds = min_request_interval_seconds
        self._last_request_at = 0.0

    async def _throttled_get(self, session: aiohttp.ClientSession, url: str) -> dict | None:
        # The free tier has no documented hard limit published, but a burst
        # of requests returns 429s (confirmed live researching this
        # feature) — a simple minimum-interval throttle is enough for a
        # background poller that isn't racing anything.
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._min_request_interval_seconds:
            await asyncio.sleep(self._min_request_interval_seconds - elapsed)
        self._last_request_at = time.monotonic()

        try:
            async with session.get(url, timeout=aiohttp.ClientTimeout(total=10.0)) as resp:
                if resp.status == 429:
                    logger.warning("geckoterminal rate-limited (429) on %s", url)
                    return None
                resp.raise_for_status()
                return await resp.json()
        except Exception as exc:
            logger.warning("geckoterminal fetch failed for %s: %s", url, exc)
            return None

    async def fetch_pools(self, chain: str, dex: str, pages: int = 1) -> list[DexPool]:
        pools: list[DexPool] = []
        async with aiohttp.ClientSession() as session:
            for page in range(1, pages + 1):
                url = f"{self.BASE_URL}/networks/{chain}/dexes/{dex}/pools?page={page}"
                payload = await self._throttled_get(session, url)
                if payload is None:
                    break
                now = time.time()
                for row in payload.get("data", []):
                    pool = _parse_pool(chain, dex, row, now)
                    if pool is not None:
                        pools.append(pool)
        return pools
