"""Bybit Spot MAINNET client — READ-ONLY (Phase 2F, user directive,
2026-08-23) — validates the mirror leg of Binance-anchored cross_exchange
opportunities (empirically: 100% of observed LUNCUSDT cross_exchange
opportunities pair Binance buy <-> Bybit sell, see the Phase 2F report).

Same discipline as app.execution.binance_account_client: no method here
places, modifies, or cancels an order. Credentials (bybit_api_key/
bybit_api_secret) are read once per signed call from get_settings() and
used only for Bybit's V5 HMAC-SHA256 request signature — never logged,
never in an exception message, never in the DB or dashboard.

Public endpoints (book ticker, order book, instrument info) work without
any credentials. Signed endpoints (fee rate, wallet balance) raise
BybitCredentialsMissing when bybit_api_key/secret aren't configured —
callers must treat that as "not verified", never invent a fee.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from app.config.settings import get_settings
from app.execution.exchange_client import ExchangeClient, ExchangeConnectivity

MAINNET_BASE_URL = "https://api.bybit.com"
REQUEST_TIMEOUT_SECONDS = 10.0
RECV_WINDOW_MS = 5000


class BybitCredentialsMissing(Exception):
    pass


@dataclass(slots=True)
class BybitBookTicker:
    symbol: str
    bid_price: float
    ask_price: float


@dataclass(slots=True)
class BybitSymbolRules:
    symbol: str
    status: str  # Bybit's own string, e.g. "Trading"
    is_tradable: bool
    min_order_qty: float
    max_order_qty: float
    qty_step: float
    min_order_amt: float | None  # Bybit's spot MIN_NOTIONAL equivalent
    tick_size: float


@dataclass(slots=True)
class BybitFeeRate:
    symbol: str
    maker_fee_rate: float
    taker_fee_rate: float
    fetched_at: float


@dataclass(slots=True)
class BybitApiKeyInfo:
    """The CALLING KEY's own configured permission scope, from
    GET /v5/user/query-api — the equivalent lesson learned from Binance's
    canWithdraw (an ACCOUNT-level flag, not a key permission): Bybit's
    read_only flag and permissions block below are what actually reflect
    this key's own scope, not any account-wide status field."""

    read_only: bool
    ip_restricted: bool
    permissions: dict
    fetched_at: float

    def has_withdrawal_permission(self) -> bool:
        """Withdrawal is the one Bybit permission category with NO
        read-only variant (confirmed against the key-creation UI's own
        text: 'Read-only permission not supported for withdrawal
        requests') — its presence in Wallet permissions always means
        real withdrawal capability was granted. Every OTHER category
        name here (SpotTrade, ContractTrade Order/Position, OptionsTrade,
        DerivativesTrade, ...) matches Bybit's internal permission
        taxonomy but grants query/view access ONLY when read_only is
        True — the name containing "Trade" does not itself mean trading
        capability, which is why this method (unlike an earlier,
        incorrect version) does not flag those."""
        wallet_perms = self.permissions.get("Wallet", [])
        return any("withdraw" in str(p).lower() for p in wallet_perms)

    def is_safely_read_only(self) -> bool:
        return self.read_only and not self.has_withdrawal_permission()


def _parse_book_ticker(data: dict, symbol: str) -> BybitBookTicker | None:
    for entry in data.get("result", {}).get("list", []):
        if entry.get("symbol") == symbol:
            return BybitBookTicker(symbol=symbol, bid_price=float(entry["bid1Price"]), ask_price=float(entry["ask1Price"]))
    return None


def _parse_symbol_rules(data: dict, symbol: str) -> BybitSymbolRules | None:
    for entry in data.get("result", {}).get("list", []):
        if entry.get("symbol") == symbol:
            lot = entry.get("lotSizeFilter", {})
            price = entry.get("priceFilter", {})
            min_order_amt = lot.get("minOrderAmt")
            status = str(entry.get("status", "Unknown"))
            return BybitSymbolRules(
                symbol=symbol,
                status=status,
                is_tradable=status.lower() == "trading",
                min_order_qty=float(lot.get("minOrderQty", 0.0)),
                max_order_qty=float(lot.get("maxOrderQty", float("inf"))),
                qty_step=float(lot.get("basePrecision", 0.0)),
                min_order_amt=float(min_order_amt) if min_order_amt is not None else None,
                tick_size=float(price.get("tickSize", 0.0)),
            )
    return None


def _parse_api_key_info(data: dict, now: float) -> BybitApiKeyInfo:
    result = data.get("result", {})
    ips = result.get("ips", [])
    return BybitApiKeyInfo(
        read_only=bool(result.get("readOnly", 0)),
        ip_restricted=bool(ips) and ips != ["*"],
        permissions=dict(result.get("permissions", {})),
        fetched_at=now,
    )


def parse_wallet_balance(data: dict, asset: str) -> float:
    """Available (free) balance of one asset from GET /v5/account/
    wallet-balance's raw response — Phase 3A's capital-pre-positioning
    check (item: 'LUNC déjà disponible sur Bybit'). Returns 0.0 (never
    raises) if the asset isn't present in any listed account."""
    for account in data.get("result", {}).get("list", []):
        for coin in account.get("coin", []):
            if coin.get("coin") == asset:
                # availableToWithdraw is Bybit's own "free, not locked in
                # an open order" figure — the correct analog of Binance's
                # balance.free, not walletBalance (which includes locked).
                raw = coin.get("availableToWithdraw") or coin.get("walletBalance") or "0"
                return float(raw) if raw else 0.0
    return 0.0


def _parse_fee_rate(data: dict, symbol: str, now: float) -> BybitFeeRate | None:
    for entry in data.get("result", {}).get("list", []):
        if entry.get("symbol") == symbol:
            return BybitFeeRate(
                symbol=symbol,
                maker_fee_rate=float(entry["makerFeeRate"]),
                taker_fee_rate=float(entry["takerFeeRate"]),
                fetched_at=now,
            )
    return None


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BybitClient(ExchangeClient):
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    async def get_book_ticker(self, symbol: str) -> BybitBookTicker | None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/market/tickers",
                params={"category": "spot", "symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_book_ticker(data, symbol)

    async def get_order_book_depth(self, symbol: str, limit: int = 50) -> dict:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/market/orderbook",
                params={"category": "spot", "symbol": symbol, "limit": limit},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def get_symbol_rules(self, symbol: str) -> BybitSymbolRules | None:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/market/instruments-info",
                params={"category": "spot", "symbol": symbol},
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_symbol_rules(data, symbol)

    def _signed_headers_and_params(self, extra: dict | None = None) -> tuple[dict, dict]:
        settings = get_settings()
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitCredentialsMissing("bybit_api_key/bybit_api_secret not configured")
        params = dict(extra or {})
        timestamp = str(int(time.time() * 1000))
        query = urlencode(params)
        payload = timestamp + settings.bybit_api_key + str(RECV_WINDOW_MS) + query
        signature = _sign(settings.bybit_api_secret, payload)
        headers = {
            "X-BAPI-API-KEY": settings.bybit_api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
        }
        return headers, params

    async def get_fee_rate(self, symbol: str) -> BybitFeeRate | None:
        """GET /v5/account/fee-rate — SIGNED, read-only. Real account
        maker/taker fee for one symbol. Raises BybitCredentialsMissing
        (never fabricates a rate) when no key is configured."""
        headers, params = self._signed_headers_and_params({"category": "spot", "symbol": symbol})
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/account/fee-rate",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_fee_rate(data, symbol, now=time.time())

    async def get_api_key_info(self) -> BybitApiKeyInfo:
        """GET /v5/user/query-api — SIGNED, read-only. The authoritative
        check for this key's own permission scope (readOnly flag +
        per-category permissions) — the Bybit equivalent of Binance's
        apiRestrictions, verified for the same reason (item 2's
        security requirement must be checked against the KEY's scope,
        not any account-wide status field)."""
        headers, params = self._signed_headers_and_params()
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/user/query-api",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_api_key_info(data, now=time.time())

    async def get_wallet_balance(self, account_type: str = "UNIFIED") -> dict:
        """GET /v5/account/wallet-balance — SIGNED, read-only. Used only
        to confirm whether capital is actually pre-positioned on this
        exchange (Phase 2F's capital-pre-positioning check) — never for
        sizing a real order."""
        headers, params = self._signed_headers_and_params({"accountType": account_type})
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/account/wallet-balance",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                return await response.json()

    async def check_connectivity(self) -> ExchangeConnectivity:
        settings = get_settings()
        credentials_configured = bool(settings.bybit_api_key and settings.bybit_api_secret)
        start = time.monotonic()
        try:
            async with aiohttp.ClientSession() as session:
                async with session.get(
                    f"{self._base_url}/v5/market/time", timeout=aiohttp.ClientTimeout(total=self._timeout_seconds)
                ) as response:
                    response.raise_for_status()
            latency_ms = (time.monotonic() - start) * 1000
            return ExchangeConnectivity(
                reachable=True,
                credentials_configured=credentials_configured,
                latency_ms=round(latency_ms, 1),
                detail="mainnet reachable (public, unauthenticated)",
            )
        except Exception as exc:
            return ExchangeConnectivity(
                reachable=False, credentials_configured=credentials_configured, latency_ms=None, detail=f"unreachable: {exc}"
            )
