"""DYNAMIC UNIVERSE — Binance ∩ Bybit tradable Spot USDT pairs (Phase 3,
user directive, 2026-08-23) — READ-ONLY.

"Ne hardcode pas ZAMA, TOM, LUNC ou une liste arbitraire": this module
replaces any fixed watchlist for LIVE trading purposes with the actual,
currently-tradable intersection of both exchanges' Spot USDT markets,
refreshed periodically so new listings appear and suspended ones drop
out automatically. Only public endpoints are used (Binance exchangeInfo,
Bybit instruments-info) — no signed calls, no order placement.
"""

import logging
import time
from dataclasses import dataclass

from app.execution.binance_account_client import BinanceAccountClient
from app.execution.bybit_client import BybitClient

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_INTERVAL_SECONDS = 300.0


@dataclass(slots=True)
class LiveUniverse:
    common_symbols: list[str]  # "BASE/USDT" — the intersection, sorted
    binance_symbol_count: int
    bybit_symbol_count: int
    fetched_at: float


def _binance_usdt_symbols(exchange_info: dict) -> set[str]:
    symbols = set()
    for entry in exchange_info.get("symbols", []):
        if (
            entry.get("quoteAsset") == "USDT"
            and entry.get("status") == "TRADING"
            and entry.get("isSpotTradingAllowed", False)
        ):
            symbols.add(f"{entry['baseAsset']}/USDT")
    return symbols


def _bybit_usdt_symbols(instruments_info: dict) -> set[str]:
    symbols = set()
    for entry in instruments_info.get("result", {}).get("list", []):
        if entry.get("quoteCoin") == "USDT" and str(entry.get("status", "")) == "Trading":
            symbols.add(f"{entry['baseCoin']}/USDT")
    return symbols


class LiveUniverseBuilder:
    def __init__(
        self,
        binance_client: BinanceAccountClient | None = None,
        bybit_client: BybitClient | None = None,
        refresh_interval_seconds: float = DEFAULT_REFRESH_INTERVAL_SECONDS,
    ) -> None:
        self._binance = binance_client or BinanceAccountClient()
        self._bybit = bybit_client or BybitClient()
        self._refresh_interval_seconds = refresh_interval_seconds
        self._cached: LiveUniverse | None = None

    async def get_universe(self, force_refresh: bool = False) -> LiveUniverse:
        now = time.time()
        if not force_refresh and self._cached is not None and now - self._cached.fetched_at < self._refresh_interval_seconds:
            return self._cached
        universe = await self._fetch_fresh(now)
        self._cached = universe
        return universe

    async def _fetch_fresh(self, now: float) -> LiveUniverse:
        binance_symbols: set[str] = set()
        bybit_symbols: set[str] = set()
        try:
            binance_info = await self._binance.get_exchange_info()
            binance_symbols = _binance_usdt_symbols(binance_info)
        except Exception as exc:
            logger.warning("live-universe: Binance exchangeInfo fetch failed: %s", exc)
        try:
            bybit_info = await self._bybit_all_usdt_instruments()
            bybit_symbols = _bybit_usdt_symbols(bybit_info)
        except Exception as exc:
            logger.warning("live-universe: Bybit instruments-info fetch failed: %s", exc)

        common = sorted(binance_symbols & bybit_symbols)
        if self._cached is not None and not common and (binance_symbols or bybit_symbols):
            # a fetch that returns an empty intersection while the PREVIOUS
            # cycle had one is far more likely a partial API failure than
            # every single common pair vanishing at once — keep serving
            # the last known-good universe rather than silently going to
            # zero eligible symbols.
            logger.warning("live-universe: fresh intersection is empty despite a non-empty previous universe — keeping the stale one, not fabricating a fresh empty universe")
            return self._cached
        return LiveUniverse(common_symbols=common, binance_symbol_count=len(binance_symbols), bybit_symbol_count=len(bybit_symbols), fetched_at=now)

    async def _bybit_all_usdt_instruments(self) -> dict:
        # Bybit's instruments-info endpoint is paginated with a cursor for
        # the full spot list (no symbol filter) — walk every page rather
        # than assuming one page covers the whole market.
        combined: dict = {"result": {"list": []}}
        cursor = None
        for _ in range(20):  # hard cap — a runaway pagination bug must never turn into an infinite loop
            page = await self._bybit_instruments_page(cursor)
            page_list = page.get("result", {}).get("list", [])
            combined["result"]["list"].extend(page_list)
            cursor = page.get("result", {}).get("nextPageCursor") or None
            if not cursor:
                break
        return combined

    async def _bybit_instruments_page(self, cursor: str | None) -> dict:
        import aiohttp

        params = {"category": "spot"}
        if cursor:
            params["cursor"] = cursor
        async with aiohttp.ClientSession() as session:
            async with session.get(
                "https://api.bybit.com/v5/market/instruments-info", params=params, timeout=aiohttp.ClientTimeout(total=10.0)
            ) as response:
                response.raise_for_status()
                return await response.json()


live_universe_builder = LiveUniverseBuilder()
