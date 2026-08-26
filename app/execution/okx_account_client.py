"""OKX Spot MAINNET account client — READ-ONLY (user directive,
2026-08-25, "integre OKX aussi"). Mirrors app.execution.binance_account_
client / app.execution.bybit_client exactly: reads real account state
(balances, trade fees) from OKX's live v5 REST API. Contains no method
that places, modifies, or cancels an order — that capability lives only
in app.execution.okx_live_trade_client, a separate module, exactly
matching the Binance/Bybit split this codebase already enforces (see
tests/test_phase3a_isolation.py).

Credential handling: okx_api_key/okx_api_secret/okx_api_passphrase are
read once per call from get_settings() (env-var/.env-backed, never
hardcoded) and used only to build the three OK-ACCESS-* signing headers.
Neither value is ever interpolated into a log line, exception message,
DB row, or dashboard payload.

OKX v5 signing (https://www.okx.com/docs-v5/en/): sign = base64(HMAC-
SHA256(secret, timestamp + method + requestPath + body)), where
timestamp is ISO8601 UTC with milliseconds ("...Z"), requestPath
includes the query string for GET, and body is the empty string for GET
(a POST's exact JSON body string for POST — see okx_live_trade_client)."""

import base64
import hashlib
import hmac
import json
import socket
import time
from dataclasses import dataclass
from datetime import UTC, datetime

import aiohttp

from app.config.settings import get_settings
from app.execution.exchange_client import ExchangeClient, ExchangeConnectivity

MAINNET_BASE_URL = "https://www.okx.com"
REQUEST_TIMEOUT_SECONDS = 10.0


class OkxCredentialsMissing(Exception):
    """Raised instead of attempting a signed call with empty credentials —
    never attempts to sign a request with an empty secret/passphrase."""


def to_okx_symbol(symbol: str) -> str:
    return symbol.replace("/", "-")


def new_okx_http_session() -> aiohttp.ClientSession:
    """Pins every OKX aiohttp session to IPv4, matching the exact IPv4
    address on file for this account's IP whitelist (account/config's
    own `ip` field). Originally added 2026-08-26 on the theory that an
    IPv6-address mismatch explained a real order-placement 401 -- kept
    as a real, still-worthwhile defensive measure, but DISCLOSED AS
    DISPROVEN as the actual root cause: the identical 401 recurred on a
    second real attempt with this fix already in place, ruling out IP
    whitelist mismatch as the (sole) explanation. See
    OkxOrderSubmissionError / read_okx_response for the actual next
    diagnostic step -- capturing OKX's own detailed error body, which
    the original incident's generic "401 Unauthorized" (aiohttp's own
    message, not OKX's) never captured."""
    connector = aiohttp.TCPConnector(family=socket.AF_INET)
    return aiohttp.ClientSession(connector=connector)


class OkxApiError(Exception):
    """Raised with OKX's own detailed error body (code + msg), never
    just the bare HTTP status -- a generic "401 Unauthorized" from
    aiohttp's own raise_for_status() carries none of OKX's own
    diagnostic detail (e.g. a specific numbered error code explaining
    WHY: bad signature, IP not whitelisted, permission missing, symbol
    not tradable for this account type, etc.), which is exactly the
    detail the 2026-08-26 order-placement 401 incident was missing."""


async def read_okx_response(response: aiohttp.ClientResponse, *, context: str) -> dict:
    """Reads the body BEFORE checking status (raise_for_status() would
    discard it) and raises OkxApiError with the FULL real response text
    on any non-2xx status, so a future incident's exception message
    carries OKX's own explanation instead of just the HTTP status code."""
    text = await response.text()
    if response.status < 200 or response.status >= 300:
        raise OkxApiError(f"{context}: HTTP {response.status} — {text}")
    return json.loads(text) if text else {}


def okx_timestamp() -> str:
    """ISO8601 UTC with millisecond precision, OKX's required format
    (e.g. '2026-08-25T12:00:00.000Z')."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def sign_request(secret: str, timestamp: str, method: str, request_path: str, body: str = "") -> str:
    """Pure. base64(HMAC-SHA256(secret, timestamp+method+requestPath+body))
    — exactly OKX's documented v5 signing scheme."""
    prehash = f"{timestamp}{method}{request_path}{body}"
    digest = hmac.new(secret.encode(), prehash.encode(), hashlib.sha256).digest()
    return base64.b64encode(digest).decode()


@dataclass(slots=True)
class OkxBalance:
    currency: str
    available: float
    frozen: float


@dataclass(slots=True)
class OkxAccountSnapshot:
    balances: list[OkxBalance]

    def balance_of(self, currency: str) -> float:
        """Available (not frozen) balance of any currency — mirrors
        BinanceAccountSnapshot.balance_of / bybit_client.parse_wallet_balance."""
        for b in self.balances:
            if b.currency == currency:
                return b.available
        return 0.0

    def balance_usdt(self) -> float:
        return self.balance_of("USDT")


@dataclass(slots=True)
class OkxTradeFee:
    inst_id: str
    maker_fee_rate: float
    taker_fee_rate: float


def _parse_account_snapshot(data: dict) -> OkxAccountSnapshot:
    balances: list[OkxBalance] = []
    for entry in data.get("data", []):
        for detail in entry.get("details", []):
            avail = detail.get("availBal")
            frozen = detail.get("frozenBal")
            if avail is None:
                continue
            balances.append(OkxBalance(currency=detail["ccy"], available=float(avail), frozen=float(frozen or 0.0)))
    return OkxAccountSnapshot(balances=balances)


def _parse_trade_fee(data: dict, inst_id: str) -> OkxTradeFee | None:
    for entry in data.get("data", []):
        maker, taker = entry.get("maker"), entry.get("taker")
        if maker is None or taker is None:
            continue
        # OKX returns fee rates as negative-for-rebate/positive-for-cost
        # strings from the taker/maker perspective inverted (a taker FEE
        # is reported as a negative number, e.g. "-0.001") -- normalize to
        # a plain positive rate, matching Binance/Bybit's convention.
        return OkxTradeFee(inst_id=inst_id, maker_fee_rate=abs(float(maker)), taker_fee_rate=abs(float(taker)))
    return None


class OkxAccountClient(ExchangeClient):
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def _signed_headers(self, method: str, request_path: str, body: str = "") -> dict:
        settings = get_settings()
        if not settings.okx_api_key or not settings.okx_api_secret or not settings.okx_api_passphrase:
            raise OkxCredentialsMissing("okx_api_key/okx_api_secret/okx_api_passphrase not configured")
        timestamp = okx_timestamp()
        return {
            "OK-ACCESS-KEY": settings.okx_api_key,
            "OK-ACCESS-SIGN": sign_request(settings.okx_api_secret, timestamp, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": settings.okx_api_passphrase,
            "Content-Type": "application/json",
        }

    async def get_account_snapshot(self) -> OkxAccountSnapshot:
        """GET /api/v5/account/balance — SIGNED, read-only. No order-
        related endpoint is ever called by this class."""
        request_path = "/api/v5/account/balance"
        headers = self._signed_headers("GET", request_path)
        async with new_okx_http_session() as session:
            async with session.get(
                f"{self._base_url}{request_path}", headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                data = await read_okx_response(response, context="get_account_snapshot")
        return _parse_account_snapshot(data)

    async def get_trade_fee(self, symbol: str) -> OkxTradeFee | None:
        """GET /api/v5/account/trade-fee — SIGNED, read-only. Real
        account-specific maker/taker fee for one spot instrument."""
        inst_id = to_okx_symbol(symbol)
        query = f"instType=SPOT&instId={inst_id}"
        request_path = f"/api/v5/account/trade-fee?{query}"
        headers = self._signed_headers("GET", request_path)
        async with new_okx_http_session() as session:
            async with session.get(
                f"{self._base_url}{request_path}", headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                data = await read_okx_response(response, context="get_trade_fee")
        return _parse_trade_fee(data, inst_id)

    async def get_book_ticker(self, symbol: str) -> dict:
        """GET /api/v5/market/ticker — public, unauthenticated. Kept here
        too (in addition to app.scanner.okx_public_client) so this class
        alone is enough for anything wiring OKX into the same shape as
        BinanceAccountClient/BybitClient."""
        inst_id = to_okx_symbol(symbol)
        async with new_okx_http_session() as session:
            async with session.get(
                f"{self._base_url}/api/v5/market/ticker", params={"instId": inst_id},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                return await read_okx_response(response, context="get_book_ticker")

    async def check_connectivity(self) -> ExchangeConnectivity:
        start = time.monotonic()
        settings = get_settings()
        try:
            await self.get_account_snapshot()
            latency_ms = (time.monotonic() - start) * 1000
            return ExchangeConnectivity(
                reachable=True, credentials_configured=True, latency_ms=round(latency_ms, 1), detail="mainnet reachable, signed call succeeded",
            )
        except OkxCredentialsMissing:
            return ExchangeConnectivity(reachable=False, credentials_configured=False, latency_ms=None, detail="credentials not configured")
        except Exception as exc:
            return ExchangeConnectivity(
                reachable=False, credentials_configured=bool(settings.okx_api_key), latency_ms=None, detail=f"unreachable or auth failed: {exc}",
            )
