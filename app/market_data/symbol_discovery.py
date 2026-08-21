"""Dynamic cross-exchange symbol universe (Opportunity Expansion spec, Step 1).

The engine's tradable-asset list (`app.config.constants.CROSS_EXCHANGE_ASSETS`)
used to be a hand-picked, hand-verified-once (2026-08-19) static list. Found
live, 2026-08-21: it's already wrong for one exchange — 8 of the 43
subscribed symbols aren't actually listed on Bybit (BNB/BTC, every FDUSD
pair), rejected on every connect, silently reducing that exchange's real
coverage by ~19% with no way to notice or self-correct.

This module replaces "a human verified it once" with "the engine checks
live, every startup" — each exchange's own public REST API is the single
source of truth for what it actually lists right now, not a list a human
copied down two days ago. Three failure-tolerant fetches (one per
exchange), each returning {symbol: 24h USDT quote volume}; a liquidity
floor keeps the discovered universe to genuinely tradable markets rather
than every long-tail pair an exchange happens to list.
"""

import asyncio
import logging
from dataclasses import dataclass, field

import aiohttp

logger = logging.getLogger(__name__)

BINANCE_TICKER_URL = "https://api.binance.com/api/v3/ticker/24hr"
OKX_TICKER_URL = "https://www.okx.com/api/v5/market/tickers?instType=SPOT"
BYBIT_TICKER_URL = "https://api.bybit.com/v5/market/tickers?category=spot"

FETCH_TIMEOUT_SECONDS = 10.0
# Below this 24h USDT quote volume, a listed pair is real but too thin to
# treat as a genuine fast-rotation candidate — matches the same "liquid
# markets only" instruction Step 1 gives, not an arbitrary cutoff: this is
# roughly the volume below which a single $1-2k VWAP fill would already eat
# most of a typical net edge in slippage alone.
MIN_QUOTE_VOLUME_USD = 500_000.0
# Bounds how many assets a successful discovery can hand back to the
# collectors/engines — protects WS subscription load (Bybit rejects a whole
# subscribe batch if even one symbol in it is invalid, and more symbols
# means more messages/bandwidth on every collector) even if hundreds of
# pairs technically clear the volume floor.
MAX_DISCOVERED_ASSETS = 60


@dataclass(slots=True)
class DiscoveryResult:
    """Per-exchange outcome — `reachable=False` means the fetch itself
    failed (network/timeout/malformed response), distinct from a fetch that
    succeeded but found nothing: callers must be able to tell "Binance is
    down right now" from "Binance genuinely lists nothing here", since only
    the first should fall back rather than shrink the universe."""

    exchange: str
    reachable: bool
    quote_volume_by_symbol: dict[str, float] = field(default_factory=dict)  # "BASE/USDT" -> 24h USD volume


@dataclass(slots=True)
class DiscoveredUniverse:
    per_exchange: dict[str, DiscoveryResult]
    # Assets (base symbol, e.g. "BTC") whose X/USDT pair is listed with
    # sufficient volume on 2 or more exchanges — this is what Step 1 asks
    # for: "symbols simultaneously tradable across 2+ exchanges".
    assets_on_2_or_more_exchanges: list[str]
    # Which exchanges actually confirmed each symbol — used to build each
    # collector's OWN subscription list instead of pushing one shared list
    # everywhere regardless of whether it's real there (the Bybit bug).
    listed_on: dict[str, set[str]]  # "BASE/USDT" -> {"binance", "okx", ...}
    degraded: bool  # True if 1+ exchange fetch failed — caller should be cautious about trusting a shrunk universe


async def _fetch_binance(session: aiohttp.ClientSession) -> DiscoveryResult:
    try:
        async with session.get(BINANCE_TICKER_URL, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)) as resp:
            resp.raise_for_status()
            rows = await resp.json()
        volumes: dict[str, float] = {}
        for row in rows:
            symbol = row.get("symbol", "")
            if not symbol.endswith("USDT") or len(symbol) <= 4:
                continue
            base = symbol[:-4]
            try:
                volumes[f"{base}/USDT"] = float(row["quoteVolume"])
            except (KeyError, TypeError, ValueError):
                continue
        return DiscoveryResult("binance", reachable=True, quote_volume_by_symbol=volumes)
    except Exception as exc:
        logger.warning("symbol discovery: binance fetch failed, falling back for this exchange: %s", exc)
        return DiscoveryResult("binance", reachable=False)


async def _fetch_okx(session: aiohttp.ClientSession) -> DiscoveryResult:
    try:
        async with session.get(OKX_TICKER_URL, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        volumes: dict[str, float] = {}
        for row in payload.get("data", []):
            inst_id = row.get("instId", "")
            if not inst_id.endswith("-USDT"):
                continue
            base = inst_id[: -len("-USDT")]
            try:
                # volCcy24h is the 24h volume in the quote currency (USDT) already.
                volumes[f"{base}/USDT"] = float(row["volCcy24h"])
            except (KeyError, TypeError, ValueError):
                continue
        return DiscoveryResult("okx", reachable=True, quote_volume_by_symbol=volumes)
    except Exception as exc:
        logger.warning("symbol discovery: okx fetch failed, falling back for this exchange: %s", exc)
        return DiscoveryResult("okx", reachable=False)


async def _fetch_bybit(session: aiohttp.ClientSession) -> DiscoveryResult:
    try:
        async with session.get(BYBIT_TICKER_URL, timeout=aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)) as resp:
            resp.raise_for_status()
            payload = await resp.json()
        volumes: dict[str, float] = {}
        for row in payload.get("result", {}).get("list", []):
            symbol = row.get("symbol", "")
            if not symbol.endswith("USDT") or len(symbol) <= 4:
                continue
            base = symbol[:-4]
            try:
                # turnover24h is the 24h volume in the quote currency (USDT).
                volumes[f"{base}/USDT"] = float(row["turnover24h"])
            except (KeyError, TypeError, ValueError):
                continue
        return DiscoveryResult("bybit", reachable=True, quote_volume_by_symbol=volumes)
    except Exception as exc:
        logger.warning("symbol discovery: bybit fetch failed, falling back for this exchange: %s", exc)
        return DiscoveryResult("bybit", reachable=False)


def build_discovered_universe(
    results: list[DiscoveryResult],
    *,
    min_quote_volume_usd: float = MIN_QUOTE_VOLUME_USD,
    max_assets: int = MAX_DISCOVERED_ASSETS,
) -> DiscoveredUniverse:
    """Pure aggregation step — every input is an already-fetched result, so
    this is unit-testable without a network call."""
    per_exchange = {r.exchange: r for r in results}
    degraded = any(not r.reachable for r in results)

    listed_on: dict[str, set[str]] = {}
    min_volume_across_listed: dict[str, float] = {}
    for result in results:
        if not result.reachable:
            continue
        for symbol, volume in result.quote_volume_by_symbol.items():
            if volume < min_quote_volume_usd:
                continue
            listed_on.setdefault(symbol, set()).add(result.exchange)
            min_volume_across_listed[symbol] = min(min_volume_across_listed.get(symbol, volume), volume)

    on_2_or_more = [symbol for symbol, exchanges in listed_on.items() if len(exchanges) >= 2]
    # Rank by the WORST (minimum) of the volumes across the exchanges that
    # list it — a pair that's deep on Binance but thin on OKX is only as
    # liquid, cross-exchange, as its thinnest leg. Kept in this order (not
    # re-sorted alphabetically) so the strongest, safest candidates are
    # both what gets kept under max_assets AND what a log line shows first.
    on_2_or_more.sort(key=lambda s: min_volume_across_listed[s], reverse=True)
    top_symbols = on_2_or_more[:max_assets]
    seen: set[str] = set()
    assets: list[str] = []
    for symbol in top_symbols:
        base = symbol.split("/")[0]
        if base not in seen:
            seen.add(base)
            assets.append(base)

    return DiscoveredUniverse(
        per_exchange=per_exchange, assets_on_2_or_more_exchanges=assets, listed_on=listed_on, degraded=degraded
    )


async def discover_symbol_universe(
    *, min_quote_volume_usd: float = MIN_QUOTE_VOLUME_USD, max_assets: int = MAX_DISCOVERED_ASSETS
) -> DiscoveredUniverse:
    """Live REST discovery across all 3 priority exchanges, run in
    parallel. Never raises — a total failure comes back as a
    `DiscoveredUniverse` with `degraded=True` and an empty asset list;
    callers must fall back to the static list themselves rather than trust
    an empty/shrunk universe as if it were a genuine "nothing is liquid
    right now" signal.
    """
    async with aiohttp.ClientSession() as session:
        results = await asyncio.gather(_fetch_binance(session), _fetch_okx(session), _fetch_bybit(session))
    return build_discovered_universe(list(results), min_quote_volume_usd=min_quote_volume_usd, max_assets=max_assets)
