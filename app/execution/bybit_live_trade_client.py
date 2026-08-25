"""Bybit Spot MAINNET client — LIVE TRADE CAPABLE (Phase 3A, user
directive, 2026-08-23).

DANGER, same posture as app.execution.binance_live_trade_client: this
module CAN place real orders (POST /v5/order/create). It exists ONLY to
serve app.execution.live_arbitrage_executor and is never imported by
main.py's detection loop.

Bybit's order-create response does NOT include fill details the way
Binance's market-order response does — it only confirms the order was
accepted (an ACK), never that it filled. Callers MUST follow up with
get_order_status before treating a submission as anything but "pending,
unknown outcome" — this is exactly the "never assume ACK means FILLED"
requirement the caller (live_arbitrage_executor) is built around.
"""

import hashlib
import hmac
import json
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import aiohttp

from app.config.settings import get_settings

MAINNET_BASE_URL = "https://api.bybit.com"
REQUEST_TIMEOUT_SECONDS = 10.0
RECV_WINDOW_MS = 5000

# Bybit's own terminal order-status strings (category=spot) — anything
# not in this set (New, PartiallyFilled, Untriggered, ...) must be
# treated as still-pending, never assumed resolved.
TERMINAL_STATUSES = {"Filled", "Cancelled", "Rejected", "PartiallyFilledCanceled", "Deactivated"}


class BybitLiveCredentialsMissing(Exception):
    pass


@dataclass(slots=True)
class BybitOrderAck:
    """What POST /v5/order/create actually returns — an acknowledgement
    that the order was accepted, NOT a fill confirmation."""

    order_id: str
    order_link_id: str
    raw: dict


@dataclass(slots=True)
class BybitOrderStatus:
    order_id: str
    order_link_id: str
    symbol: str
    side: str
    order_status: str  # Bybit's own string
    cum_exec_qty: float
    cum_exec_value: float
    # cum_exec_fee is Bybit-deprecated for spot ("Use cumFeeDetail
    # instead" per their own v5 docs) and NEVER carries a currency —
    # kept only for raw/debug visibility. cum_fee_detail (2026-08-24
    # fix, after the first real fill mislabeled a 2.9179 RVN fee as
    # $2.9179) is the authoritative, currency-aware source: never
    # assume a Bybit fee is in USDT.
    cum_exec_fee: float
    avg_price: float | None
    raw: dict
    cum_fee_detail: dict[str, float] = field(default_factory=dict)

    @property
    def is_filled(self) -> bool:
        return self.order_status == "Filled"

    @property
    def is_partially_filled(self) -> bool:
        return self.order_status in ("PartiallyFilled", "PartiallyFilledCanceled")

    @property
    def is_terminal(self) -> bool:
        return self.order_status in TERMINAL_STATUSES

    def total_fees_by_asset(self) -> dict[str, float]:
        """Mirrors BinanceOrderResult.total_fees_by_asset()'s name/shape
        for symmetry — callers must resolve currency from this, never
        from the bare, currency-less cum_exec_fee."""
        return dict(self.cum_fee_detail)


def _parse_order_ack(data: dict) -> BybitOrderAck:
    result = data.get("result", {})
    return BybitOrderAck(order_id=str(result.get("orderId", "")), order_link_id=str(result.get("orderLinkId", "")), raw=data)


def _parse_one_order_status(entry: dict) -> BybitOrderStatus:
    avg_price = entry.get("avgPrice")
    fee_detail_raw = entry.get("cumFeeDetail") or {}
    cum_fee_detail = {str(asset): float(amount) for asset, amount in fee_detail_raw.items()}
    return BybitOrderStatus(
        order_id=str(entry.get("orderId", "")),
        order_link_id=str(entry.get("orderLinkId", "")),
        symbol=str(entry.get("symbol", "")),
        side=str(entry.get("side", "")),
        order_status=str(entry.get("orderStatus", "")),
        cum_exec_qty=float(entry.get("cumExecQty", 0.0) or 0.0),
        cum_exec_value=float(entry.get("cumExecValue", 0.0) or 0.0),
        cum_exec_fee=float(entry.get("cumExecFee", 0.0) or 0.0),
        avg_price=float(avg_price) if avg_price else None,
        raw=entry,
        cum_fee_detail=cum_fee_detail,
    )


def _parse_order_status(data: dict) -> BybitOrderStatus | None:
    for entry in data.get("result", {}).get("list", []):
        return _parse_one_order_status(entry)
    return None


def _parse_order_status_list(data: dict) -> list[BybitOrderStatus]:
    return [_parse_one_order_status(entry) for entry in data.get("result", {}).get("list", [])]


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


class BybitLiveTradeClient:
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def _signed_headers(self, timestamp: str, payload: str) -> dict:
        settings = get_settings()
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitLiveCredentialsMissing("bybit_api_key/bybit_api_secret not configured")
        signature = _sign(settings.bybit_api_secret, payload)
        return {
            "X-BAPI-API-KEY": settings.bybit_api_key,
            "X-BAPI-TIMESTAMP": timestamp,
            "X-BAPI-SIGN": signature,
            "X-BAPI-RECV-WINDOW": str(RECV_WINDOW_MS),
            "Content-Type": "application/json",
        }

    async def place_market_order(self, symbol: str, side: str, qty: float, order_link_id: str, market_unit: str | None = None) -> BybitOrderAck:
        """POST /v5/order/create — SIGNED, category=spot, orderType=Market.

        qty's meaning depends on market_unit (verified against the
        current Bybit v5 docs, 2026-08-24, after a real order was
        rejected with retCode=170003 "An unknown parameter was sent" —
        the previous version of this method never set
        marketUnit/isLeverage/orderFilter at all):
          - marketUnit="quoteCoin": qty is the QUOTE-currency (USDT)
            notional to spend. Bybit fills as much base asset as that
            USDT amount buys at the real execution price.
          - marketUnit="baseCoin": qty is the BASE-asset quantity to
            trade. The caller is responsible for rounding qty to the
            real LOT_SIZE step already fetched via
            app.execution.bybit_client.

        market_unit defaults to side's own natural convention when not
        given explicitly — Buy defaults to "quoteCoin" (inventory
        constitution: spend up to a USDT cap), Sell is always
        "baseCoin". An arbitrage buy leg (item 1, user directive,
        2026-08-24, post-incident: common dual-leg sizing) must pass
        market_unit="baseCoin" explicitly to be quantity-capped instead
        of notional-capped — notional-capping a buy can silently acquire
        more base asset than the sell leg can actually absorb.

        Also sends isLeverage=0 (explicit: non-margin spot) and
        orderFilter="Order" (explicit: a plain order, not a TP/SL or
        conditional order) — both optional per Bybit's docs but sent
        explicitly rather than relying on defaults. Returns only an ACK;
        call get_order_status next, always."""
        settings = get_settings()
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitLiveCredentialsMissing("bybit_api_key/bybit_api_secret not configured")
        normalized_side = side.capitalize()
        if normalized_side not in ("Buy", "Sell"):
            raise ValueError(f"side must be 'Buy' or 'Sell', got {side!r}")
        if market_unit is None:
            market_unit = "quoteCoin" if normalized_side == "Buy" else "baseCoin"
        elif market_unit not in ("quoteCoin", "baseCoin"):
            raise ValueError(f"market_unit must be 'quoteCoin' or 'baseCoin', got {market_unit!r}")
        body = {
            "category": "spot",
            "symbol": symbol,
            "side": normalized_side,
            "orderType": "Market",
            "qty": f"{qty:.8f}".rstrip("0").rstrip("."),
            "marketUnit": market_unit,
            "isLeverage": 0,
            "orderFilter": "Order",
            "orderLinkId": order_link_id,
        }
        body_json = json.dumps(body, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        payload = timestamp + settings.bybit_api_key + str(RECV_WINDOW_MS) + body_json
        headers = self._signed_headers(timestamp, payload)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                f"{self._base_url}/v5/order/create",
                headers=headers,
                data=body_json,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        if data.get("retCode") != 0:
            # Keeps the full response (retExtInfo etc.), not just
            # retCode/retMsg — the earlier version of this error dropped
            # everything but those two fields, which was not enough
            # detail to pin down the exact cause of a real rejection.
            raise RuntimeError(f"Bybit order-create rejected: retCode={data.get('retCode')} retMsg={data.get('retMsg')} full_response={data}")
        return _parse_order_ack(data)

    async def get_order_status(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None) -> BybitOrderStatus | None:
        """GET /v5/order/realtime — SIGNED. Checks OPEN/recent orders
        first; a terminal (filled/cancelled/rejected) order may have
        already rolled off this endpoint, in which case callers should
        fall back to get_order_history."""
        if (order_id is None) == (order_link_id is None):
            raise ValueError("exactly one of order_id or order_link_id must be given")
        params: dict = {"category": "spot", "symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if order_link_id is not None:
            params["orderLinkId"] = order_link_id
        query = urlencode(params)
        timestamp = str(int(time.time() * 1000))
        settings = get_settings()
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitLiveCredentialsMissing("bybit_api_key/bybit_api_secret not configured")
        payload = timestamp + settings.bybit_api_key + str(RECV_WINDOW_MS) + query
        headers = self._signed_headers(timestamp, payload)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/order/realtime",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_order_status(data)

    async def get_open_orders(self, symbol: str | None = None) -> list[BybitOrderStatus]:
        """GET /v5/order/realtime — SIGNED, read-only, called with NEITHER
        orderId NOR orderLinkId (a documented, valid Bybit v5 call shape
        that returns every currently open order for the category,
        optionally scoped to one symbol). Added for AUTONOMOUS 24/7
        startup safety (user directive, 2026-08-25) -- see
        BinanceLiveTradeClient.get_open_orders's docstring for the full
        rationale: any order found here at startup is inherently
        anomalous under this system's own design (MAX_CONCURRENT=1,
        always polled to terminal before moving on) and must never be
        silently assumed resolved either way."""
        params: dict = {"category": "spot"}
        if symbol is not None:
            params["symbol"] = symbol
        query = urlencode(params)
        timestamp = str(int(time.time() * 1000))
        settings = get_settings()
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitLiveCredentialsMissing("bybit_api_key/bybit_api_secret not configured")
        payload = timestamp + settings.bybit_api_key + str(RECV_WINDOW_MS) + query
        headers = self._signed_headers(timestamp, payload)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/order/realtime",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_order_status_list(data)

    async def get_order_history(self, symbol: str, order_id: str | None = None, order_link_id: str | None = None) -> BybitOrderStatus | None:
        """GET /v5/order/history — SIGNED. Fallback for an order that has
        already rolled off the realtime/open-orders endpoint."""
        if (order_id is None) == (order_link_id is None):
            raise ValueError("exactly one of order_id or order_link_id must be given")
        params: dict = {"category": "spot", "symbol": symbol}
        if order_id is not None:
            params["orderId"] = order_id
        if order_link_id is not None:
            params["orderLinkId"] = order_link_id
        query = urlencode(params)
        timestamp = str(int(time.time() * 1000))
        settings = get_settings()
        if not settings.bybit_api_key or not settings.bybit_api_secret:
            raise BybitLiveCredentialsMissing("bybit_api_key/bybit_api_secret not configured")
        payload = timestamp + settings.bybit_api_key + str(RECV_WINDOW_MS) + query
        headers = self._signed_headers(timestamp, payload)
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{self._base_url}/v5/order/history",
                headers=headers,
                params=params,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                response.raise_for_status()
                data = await response.json()
        return _parse_order_status(data)
