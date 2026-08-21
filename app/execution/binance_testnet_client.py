"""Binance Spot Testnet client (Reality Engine spec, section 57).

check_connectivity() only ever calls Binance Testnet's public,
unauthenticated GET /api/v3/ping — confirms the testnet environment is
reachable and measures round-trip latency, nothing more. It deliberately
does NOT implement account balance or order placement, since those require
signing a request with an API secret and would be the actual crossing
point into "this process can act on funds" — even fake testnet funds are
out of scope until the user explicitly asks to build that.

Whether BINANCE_TESTNET_API_KEY/SECRET are configured in settings only
flips `credentials_configured` in the result to True — those values are
never read into a request here.
"""

import time

import aiohttp

from app.config.settings import get_settings
from app.execution.exchange_client import ExchangeClient, ExchangeConnectivity

TESTNET_BASE_URL = "https://testnet.binance.vision"
PING_TIMEOUT_SECONDS = 5.0


class BinanceTestnetClient(ExchangeClient):
    def __init__(self, base_url: str = TESTNET_BASE_URL, timeout_seconds: float = PING_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    async def check_connectivity(self) -> ExchangeConnectivity:
        settings = get_settings()
        credentials_configured = bool(settings.binance_testnet_api_key and settings.binance_testnet_api_secret)

        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/api/v3/ping", timeout=aiohttp.ClientTimeout(total=self._timeout_seconds)
                ) as response:
                    response.raise_for_status()
            latency_ms = (time.monotonic() - start) * 1000
            return ExchangeConnectivity(
                reachable=True,
                credentials_configured=credentials_configured,
                latency_ms=round(latency_ms, 1),
                detail="testnet reachable (public ping, unauthenticated)",
            )
        except Exception as exc:
            return ExchangeConnectivity(
                reachable=False, credentials_configured=credentials_configured, latency_ms=None, detail=f"unreachable: {exc}"
            )
