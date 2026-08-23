"""Binance Spot MAINNET client — LIVE TRADE CAPABLE (Phase 3A, user
directive, 2026-08-23).

DANGER, unlike every other module in app/execution/: this one CAN place
and cancel real orders (POST/DELETE /api/v3/order). It exists ONLY to
serve app.execution.live_arbitrage_executor, and is never imported by
main.py's detection loop or any other automatically-running code path —
grep for "binance_live_trade_client" outside app/execution/ and tests/ to
confirm.

Every call here is refused unless app.execution.live_guard.live_guard
allows it first (LIVE_TRADING_ENABLED must be explicitly True — a
setting this codebase never sets itself, only an operator can, outside
this process, in .env). This module does not check that guard itself —
app.execution.live_arbitrage_executor is responsible for checking it
BEFORE ever constructing a call here, so the guard check is never
bypassable by adding a second caller that forgets to check it: there is
exactly one caller, and it is reviewed for that specific property in
tests/test_phase3a_isolation.py.

No client_order_id is generated here — idempotent retry/dedup logic
belongs entirely to the caller, which must supply a fresh, unique one for
every distinct order intent and never for a retry of an ambiguous one.
"""

import hashlib
import hmac
import time
from dataclasses import dataclass
from urllib.parse import urlencode

import aiohttp

from app.config.settings import get_settings

MAINNET_BASE_URL = "https://api.binance.com"
REQUEST_TIMEOUT_SECONDS = 10.0
RECV_WINDOW_MS = 5000


class BinanceLiveCredentialsMissing(Exception):
    pass


@dataclass(slots=True)
class BinanceOrderFill:
    price: float
    qty: float
    commission: float
    commission_asset: str


@dataclass(slots=True)
class BinanceOrderResult:
    symbol: str
    order_id: int
    client_order_id: str
    status: str  # Binance's own string: NEW | PARTIALLY_FILLED | FILLED | CANCELED | REJECTED | EXPIRED
    executed_qty: float
    cumulative_quote_qty: float
    fills: list[BinanceOrderFill]
    raw: dict  # full response, kept for the profit reality ledger's audit trail

    @property
    def is_filled(self) -> bool:
        return self.status == "FILLED"

    @property
    def is_partially_filled(self) -> bool:
        return self.status == "PARTIALLY_FILLED"

    @property
    def is_terminal(self) -> bool:
        """True once Binance will never change this order's state again
        (fully resolved, one way or another) — callers must not treat
        NEW as terminal, and must poll get_order_status until this is
        true or a strict timeout elapses."""
        return self.status in ("FILLED", "CANCELED", "REJECTED", "EXPIRED")

    def average_fill_price(self) -> float | None:
        if self.executed_qty <= 0:
            return None
        return self.cumulative_quote_qty / self.executed_qty

    def total_fees_by_asset(self) -> dict[str, float]:
        totals: dict[str, float] = {}
        for fill in self.fills:
            totals[fill.commission_asset] = totals.get(fill.commission_asset, 0.0) + fill.commission
        return totals


def _parse_order_result(data: dict) -> BinanceOrderResult:
    fills = [
        BinanceOrderFill(
            price=float(f["price"]), qty=float(f["qty"]), commission=float(f["commission"]), commission_asset=f["commissionAsset"]
        )
        for f in data.get("fills", [])
    ]
    return BinanceOrderResult(
        symbol=data["symbol"],
        order_id=int(data["orderId"]),
        client_order_id=str(data.get("clientOrderId", "")),
        status=str(data["status"]),
        executed_qty=float(data.get("executedQty", 0.0)),
        cumulative_quote_qty=float(data.get("cummulativeQuoteQty", 0.0)),
        fills=fills,
        raw=data,
    )


def _sign(secret: str, query: str) -> str:
    return hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()


class BinanceLiveTradeClient:
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def _signed_request_params(self, extra: dict | None = None) -> tuple[dict, dict]:
        settings = get_settings()
        if not settings.binance_api_key or not settings.binance_api_secret:
            raise BinanceLiveCredentialsMissing("binance_api_key/binance_api_secret not configured")
        params = {"timestamp": int(time.time() * 1000), "recvWindow": RECV_WINDOW_MS}
        if extra:
            params.update(extra)
        query = urlencode(params)
        params["signature"] = _sign(settings.binance_api_secret, query)
        headers = {"X-MBX-APIKEY": settings.binance_api_key}
        return headers, params

    async def place_market_order(
        self,
        symbol: str,
        side: str,
        client_order_id: str,
        quantity: float | None = None,
        quote_order_qty: float | None = None,
    ) -> BinanceOrderResult:
        """POST /api/v3/order — SIGNED. Exactly one of quantity (base
        asset amount) or quote_order_qty (quote asset amount, e.g. USDT
        for a BUY) must be given. Binance market orders are filled
        synchronously in the vast majority of cases, but the returned
        status must still be checked (not assumed FILLED) — see
        BinanceOrderResult.is_terminal."""
        if (quantity is None) == (quote_order_qty is None):
            raise ValueError("exactly one of quantity or quote_order_qty must be given")
        extra: dict = {"symbol": symbol, "side": side.upper(), "type": "MARKET", "newClientOrderId": client_order_id}
        if quantity is not None:
            extra["quantity"] = f"{quantity:.8f}".rstrip("0").rstrip(".")
        if quote_order_qty is not None:
            extra["quoteOrderQty"] = f"{quote_order_qty:.8f}".rstrip("0").rstrip(".")
        headers, params = self._signed_request_params(extra)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/api/v3/order",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_order_result(data)

    async def get_order_status(
        self, symbol: str, order_id: int | None = None, orig_client_order_id: str | None = None
    ) -> BinanceOrderResult:
        """GET /api/v3/order — SIGNED. The only reliable way to confirm
        an order's terminal state after submission or after an ambiguous
        network error on the placement call itself (never assume a
        timed-out placement call means NOT placed — always reconcile via
        this before considering any retry)."""
        if (order_id is None) == (orig_client_order_id is None):
            raise ValueError("exactly one of order_id or orig_client_order_id must be given")
        extra: dict = {"symbol": symbol}
        if order_id is not None:
            extra["orderId"] = order_id
        if orig_client_order_id is not None:
            extra["origClientOrderId"] = orig_client_order_id
        headers, params = self._signed_request_params(extra)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/api/v3/order",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_order_result(data)
