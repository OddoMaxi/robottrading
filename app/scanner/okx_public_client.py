"""OKX public market-data client (user directive, 2026-08-23) — READ-ONLY,
UNAUTHENTICATED. No order-placement method exists here, nor could one:
every endpoint used is public (no signature, no API key required) —
settings.okx_api_key is empty in this deployment and none of these calls
would use it even if it were set.

Built specifically for app.scanner's live multi-exchange comparison; not
used by main.py's detection loop (that uses app.collectors.okx's
WebSocket collectors for the already-tracked universe instead).
"""

from dataclasses import dataclass

import aiohttp

from app.execution.exchange_client import ExchangeClient, ExchangeConnectivity

MAINNET_BASE_URL = "https://www.okx.com"
REQUEST_TIMEOUT_SECONDS = 10.0

# OKX's own documented VIP0 (non-VIP, non-discounted) spot regular-user
# schedule — used only as an ESTIMATED fee since no account credentials
# are configured to fetch a real, account-specific rate.
OKX_ESTIMATED_TAKER_FEE_RATE = 0.001
OKX_ESTIMATED_MAKER_FEE_RATE = 0.0008


def to_okx_symbol(symbol: str) -> str:
    return symbol.replace("/", "-")


@dataclass(slots=True)
class OkxBookTicker:
    inst_id: str
    bid_price: float
    ask_price: float


@dataclass(slots=True)
class OkxSymbolRules:
    inst_id: str
    is_tradable: bool
    min_qty: float
    lot_size: float
    tick_size: float


def _parse_book_ticker(data: dict, inst_id: str) -> OkxBookTicker | None:
    for entry in data.get("data", []):
        if entry.get("instId") == inst_id:
            bid, ask = entry.get("bidPx"), entry.get("askPx")
            if not bid or not ask:
                return None
            return OkxBookTicker(inst_id=inst_id, bid_price=float(bid), ask_price=float(ask))
    return None


def _parse_symbol_rules(data: dict, inst_id: str) -> OkxSymbolRules | None:
    for entry in data.get("data", []):
        if entry.get("instId") == inst_id:
            return OkxSymbolRules(
                inst_id=inst_id,
                is_tradable=str(entry.get("state", "")) == "live",
                min_qty=float(entry.get("minSz", 0.0)),
                lot_size=float(entry.get("lotSz", 0.0)),
                tick_size=float(entry.get("tickSz", 0.0)),
            )
    return None


class OkxPublicClient(ExchangeClient):
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    async def get_book_ticker(self, symbol: str) -> OkxBookTicker | None:
        inst_id = to_okx_symbol(symbol)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v5/market/ticker",
                params={"instId": inst_id},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_book_ticker(data, inst_id)

    async def get_order_book_depth(self, symbol: str, limit: int = 20) -> dict:
        inst_id = to_okx_symbol(symbol)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v5/market/books",
                params={"instId": inst_id, "sz": limit},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def get_symbol_rules(self, symbol: str) -> OkxSymbolRules | None:
        inst_id = to_okx_symbol(symbol)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v5/public/instruments",
                params={"instType": "SPOT", "instId": inst_id},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_symbol_rules(data, inst_id)

    async def check_connectivity(self) -> ExchangeConnectivity:
        import time

        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/v5/public/time", timeout=aiohttp.ClientTimeout(total=self._timeout_seconds)
                ) as response:
                    response.raise_for_status()
            latency_ms = (time.monotonic() - start) * 1000
            return ExchangeConnectivity(
                reachable=True, credentials_configured=False, latency_ms=round(latency_ms, 1), detail="mainnet reachable (public, unauthenticated)"
            )
        except Exception as exc:
            return ExchangeConnectivity(reachable=False, credentials_configured=False, latency_ms=None, detail=f"unreachable: {exc}")
