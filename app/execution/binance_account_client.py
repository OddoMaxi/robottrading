"""Binance Spot MAINNET account client — READ-ONLY (Phase 2D, item 3,
user directive, 2026-08-23).

Reads REAL account state from Binance's live REST API: available/locked
balances, account status (canTrade/canWithdraw/canDeposit), account type,
and permissions. This module contains no method that places, modifies, or
cancels an order — there is no function here that could construct a
POST/DELETE to an order endpoint. That is a structural guarantee, checked
by tests/test_phase2d_isolation.py, not just a docstring promise.

Credential handling (item 2): binance_api_key/binance_api_secret are read
once per call from get_settings() (env-var/`.env`-backed Pydantic
settings — never hardcoded) and used only to (a) set the X-MBX-APIKEY
header and (b) compute an HMAC-SHA256 request signature. Neither value is
ever interpolated into a log line, an exception message, a DB row, or a
dashboard payload — every error path below is built from the HTTP status
code and response body only, never from the request headers or the raw
credential values.

Network I/O (the _fetch_* methods) is deliberately kept thin and
separate from parsing (the pure `_parse_*` functions) so the parsing
logic — the part with actual decision-relevant behavior — is unit
testable without a live network call or mocked HTTP client.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from app.config.settings import get_settings
from app.execution.exchange_client import ExchangeClient, ExchangeConnectivity

MAINNET_BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT_SECONDS = 10.0
RECV_WINDOW_MS = 5000


class BinanceCredentialsMissing(Exception):
    """Raised instead of attempting a signed call with empty credentials —
    never attempts to sign a request with an empty secret."""


@dataclass(slots=True)
class BinanceBalance:
    asset: str
    free: float
    locked: float


@dataclass(slots=True)
class BinanceAccountSnapshot:
    balances: list[BinanceBalance]
    can_trade: bool
    can_withdraw: bool
    can_deposit: bool
    account_type: str
    permissions: list[str]
    fetched_at: float

    def balance_usdt(self) -> float:
        """Available (free, not locked) USDT — the only balance micro-live
        sizing may ever look at (item 3: use what Binance actually
        returns, never assume 10 USDT)."""
        for balance in self.balances:
            if balance.asset == "USDT":
                return balance.free
        return 0.0


@dataclass(slots=True)
class BinanceApiKeyRestrictions:
    """The CALLING KEY's own configured permission scope — this, not
    BinanceAccountSnapshot.can_withdraw, is what item 2's 'no withdrawal
    permission' requirement must be checked against."""

    enable_reading: bool
    enable_withdrawals: bool
    enable_spot_and_margin_trading: bool
    enable_margin: bool
    enable_futures: bool
    enable_internal_transfer: bool
    ip_restrict: bool
    fetched_at: float


def _parse_api_restrictions(data: dict, now: float) -> BinanceApiKeyRestrictions:
    return BinanceApiKeyRestrictions(
        enable_reading=bool(data.get("enableReading", False)),
        enable_withdrawals=bool(data.get("enableWithdrawals", False)),
        enable_spot_and_margin_trading=bool(data.get("enableSpotAndMarginTrading", False)),
        enable_margin=bool(data.get("enableMargin", False)),
        enable_futures=bool(data.get("enableFutures", False)),
        enable_internal_transfer=bool(data.get("enableInternalTransfer", False)),
        ip_restrict=bool(data.get("ipRestrict", False)),
        fetched_at=now,
    )


def _parse_account_snapshot(data: dict, now: float) -> BinanceAccountSnapshot:
    balances = [
        BinanceBalance(asset=b["asset"], free=float(b["free"]), locked=float(b["locked"]))
        for b in data.get("balances", [])
        if float(b["free"]) > 0 or float(b["locked"]) > 0
    ]
    return BinanceAccountSnapshot(
        balances=balances,
        can_trade=bool(data.get("canTrade", False)),
        can_withdraw=bool(data.get("canWithdraw", False)),
        can_deposit=bool(data.get("canDeposit", False)),
        account_type=str(data.get("accountType", "unknown")),
        permissions=list(data.get("permissions", [])),
        fetched_at=now,
    )


def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceAccountClient(ExchangeClient):
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def _signed_request_params(self, extra: dict | None = None) -> tuple[dict, dict]:
        settings = get_settings()
        if not settings.binance_api_key or not settings.binance_api_secret:
            raise BinanceCredentialsMissing("binance_api_key/binance_api_secret not configured")
        params = {"timestamp": int(time.time() * 1000), "recvWindow": RECV_WINDOW_MS}
        if extra:
            params.update(extra)
        query = urlencode(params)
        params["signature"] = _sign(settings.binance_api_secret, query)
        headers = {"X-MBX-APIKEY": settings.binance_api_key}
        return headers, params

    async def get_account_snapshot(self) -> BinanceAccountSnapshot:
        """GET /api/v3/account — SIGNED, read-only. No order-related
        endpoint is ever called by this class."""
        headers, params = self._signed_request_params()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v3/account",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_account_snapshot(data, now=time.time())

    async def get_exchange_info(self, symbols: list[str] | None = None) -> dict:
        """GET /api/v3/exchangeInfo — public, unauthenticated. Returns the
        raw JSON; app.execution.binance_filters parses it."""
        params: dict = {}
        if symbols:
            params["symbols"] = "[" + ",".join(f'"{s}"' for s in symbols) + "]"
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v3/exchangeInfo",
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def get_book_ticker(self, symbol: str) -> dict:
        """GET /api/v3/ticker/bookTicker — public, best bid/ask."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v3/ticker/bookTicker",
                params={"symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def get_order_book_depth(self, symbol: str, limit: int = 20) -> dict:
        """GET /api/v3/depth — public, top-of-book depth for slippage
        estimation."""
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v3/depth",
                params={"symbol": symbol, "limit": limit},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def get_api_restrictions(self) -> BinanceApiKeyRestrictions:
        """GET /sapi/v1/account/apiRestrictions — SIGNED, read-only. This
        is the endpoint that actually reflects the CALLING KEY's own
        configured permission scope (enableReading/enableWithdrawals/
        enableSpotAndMarginTrading/ipRestrict, etc.) — unlike
        /api/v3/account's canWithdraw, which reflects the ACCOUNT's
        overall withdrawal eligibility (KYC/compliance status), not this
        key's permissions. Item 2's 'no withdrawal permission' constraint
        must be checked against enableWithdrawals from THIS endpoint."""
        headers, params = self._signed_request_params()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/sapi/v1/account/apiRestrictions",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_api_restrictions(data, now=time.time())

    async def get_trade_fee(self, symbol: str) -> dict:
        """GET /sapi/v1/asset/tradeFee — SIGNED, read-only. Real maker/
        taker fee for one symbol where Binance makes it determinable this
        way (item 4: 'fees where determinable'); callers must treat a
        failure here as ESTIMATED-fee territory, not a hard error."""
        headers, params = self._signed_request_params({"symbol": symbol})
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/sapi/v1/asset/tradeFee",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def check_connectivity(self) -> ExchangeConnectivity:
        settings = get_settings()
        credentials_configured = bool(settings.binance_api_key and settings.binance_api_secret)
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
                detail="mainnet reachable (public ping, unauthenticated)",
            )
        except Exception as exc:
            return ExchangeConnectivity(
                reachable=False, credentials_configured=credentials_configured, latency_ms=None, detail=f"unreachable: {exc}"
            )
