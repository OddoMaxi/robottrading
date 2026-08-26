"""OKX Spot MAINNET LIVE TRADE client — order-placement-capable (user
directive, 2026-08-25, "integre OKX aussi", scope: full trading
capability). Mirrors app.execution.binance_live_trade_client /
app.execution.bybit_live_trade_client exactly.

SAFETY (matches this codebase's standing invariant for every live-trade
client, see tests/test_phase3a_isolation.py): this module is dangerous
by construction (it can place real orders with real money) and MUST stay
structurally unreachable except through an explicitly authorized
executor that checks its own guard before ever calling into it — exactly
the same discipline app.execution.live_arbitrage_executor /
app.execution.inventory_constitution_executor already apply to
BinanceLiveTradeClient/BybitLiveTradeClient. As of this commit, NO file
imports this module — it exists, tested, but is not yet wired into any
executor or live trading loop. Wiring OKX into real 3-exchange
arbitrage execution is a separate, explicitly-authorized step, not
implied by this module's mere existence (matching this project's
standing rule: build real-money capability freely, never wire real
execution without a separate, explicit go-ahead)."""

import json
import uuid
from dataclasses import dataclass

import aiohttp

from app.config.settings import get_settings
from app.execution.okx_account_client import new_okx_http_session, okx_timestamp, read_okx_response, sign_request, to_okx_symbol

MAINNET_BASE_URL = "https://www.okx.com"
REQUEST_TIMEOUT_SECONDS = 10.0


class OkxLiveCredentialsMissing(Exception):
    """Raised instead of attempting a signed call with empty credentials —
    never attempts to sign a request with an empty secret/passphrase.
    Deliberately its own class, separate from
    okx_account_client.OkxCredentialsMissing, matching
    BinanceLiveCredentialsMissing/BybitLiveCredentialsMissing's own
    separation from their respective account-client exceptions."""


@dataclass(slots=True)
class OkxOrderAck:
    """What POST /api/v5/trade/order actually returns — an
    acknowledgement that the order was ACCEPTED, NOT a fill
    confirmation."""

    order_id: str
    client_order_id: str
    accepted: bool
    status_code: str
    status_message: str
    raw: dict


@dataclass(slots=True)
class OkxOrderStatus:
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    state: str  # OKX's own string: "live" | "partially_filled" | "filled" | "canceled"
    filled_qty: float
    avg_fill_price: float | None
    fee_amount: float
    fee_asset: str | None
    raw: dict

    @property
    def is_filled(self) -> bool:
        return self.state == "filled"

    @property
    def is_terminal(self) -> bool:
        return self.state in ("filled", "canceled")


def _new_client_order_id(prefix: str) -> str:
    """OKX clOrdId: alphanumeric only, max 32 characters — no hyphens
    allowed (unlike Binance/Bybit's clientOrderId/orderLinkId)."""
    return f"{prefix}{uuid.uuid4().hex}"[:32]


def _parse_order_ack(data: dict) -> OkxOrderAck:
    entry = data.get("data", [{}])[0] if data.get("data") else {}
    return OkxOrderAck(
        order_id=str(entry.get("ordId", "")), client_order_id=str(entry.get("clOrdId", "")),
        accepted=str(entry.get("sCode", "")) == "0", status_code=str(entry.get("sCode", "")),
        status_message=str(entry.get("sMsg", "")), raw=entry,
    )


def _parse_order_status(entry: dict) -> OkxOrderStatus:
    fee_raw = entry.get("fee")
    fee_ccy = entry.get("feeCcy")
    avg_px = entry.get("avgPx")
    return OkxOrderStatus(
        order_id=str(entry.get("ordId", "")), client_order_id=str(entry.get("clOrdId", "")),
        symbol=str(entry.get("instId", "")), side=str(entry.get("side", "")), state=str(entry.get("state", "")),
        filled_qty=float(entry.get("accFillSz") or 0.0), avg_fill_price=(float(avg_px) if avg_px else None),
        fee_amount=abs(float(fee_raw)) if fee_raw else 0.0, fee_asset=fee_ccy, raw=entry,
    )


class OkxLiveTradeClient:
    def __init__(self, base_url: str = MAINNET_BASE_URL, timeout_seconds: float = REQUEST_TIMEOUT_SECONDS) -> None:
        self._base_url = base_url
        self._timeout_seconds = timeout_seconds

    def _signed_headers(self, method: str, request_path: str, body: str = "") -> dict:
        settings = get_settings()
        if not settings.okx_api_key or not settings.okx_api_secret or not settings.okx_api_passphrase:
            raise OkxLiveCredentialsMissing("okx_api_key/okx_api_secret/okx_api_passphrase not configured")
        timestamp = okx_timestamp()
        return {
            "OK-ACCESS-KEY": settings.okx_api_key,
            "OK-ACCESS-SIGN": sign_request(settings.okx_api_secret, timestamp, method, request_path, body),
            "OK-ACCESS-TIMESTAMP": timestamp,
            "OK-ACCESS-PASSPHRASE": settings.okx_api_passphrase,
            "Content-Type": "application/json",
        }

    async def place_market_order(self, symbol: str, side: str, quantity: float, client_order_id: str | None = None) -> OkxOrderAck:
        """POST /api/v5/trade/order — SIGNED, tdMode=cash (spot, no
        margin/leverage — matches this project's hard "spot only, no
        leverage" limit), ordType=market. tgtCcy=base_ccy is set
        EXPLICITLY for both buy and sell (OKX's own default differs by
        side otherwise) so `quantity` always means the BASE asset amount,
        matching every other live-trade client in this codebase."""
        if side.lower() not in ("buy", "sell"):
            raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")
        inst_id = to_okx_symbol(symbol)
        coid = client_order_id or _new_client_order_id("okx")
        body_dict = {
            "instId": inst_id, "tdMode": "cash", "side": side.lower(), "ordType": "market",
            "sz": str(quantity), "tgtCcy": "base_ccy", "clOrdId": coid,
        }
        body = json.dumps(body_dict, separators=(",", ":"))
        request_path = "/api/v5/trade/order"
        headers = self._signed_headers("POST", request_path, body)
        async with new_okx_http_session() as session:
            async with session.post(
                f"{self._base_url}{request_path}", headers=headers, data=body,
                timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                data = await read_okx_response(response, context=f"place_market_order({symbol}, {side})")
        return _parse_order_ack(data)

    async def get_order_status(self, symbol: str, order_id: str | None = None, client_order_id: str | None = None) -> OkxOrderStatus | None:
        """GET /api/v5/trade/order — SIGNED. Exactly one of order_id/
        client_order_id must be given."""
        if (order_id is None) == (client_order_id is None):
            raise ValueError("exactly one of order_id or client_order_id must be given")
        inst_id = to_okx_symbol(symbol)
        query = f"instId={inst_id}&" + (f"ordId={order_id}" if order_id else f"clOrdId={client_order_id}")
        request_path = f"/api/v5/trade/order?{query}"
        headers = self._signed_headers("GET", request_path)
        async with new_okx_http_session() as session:
            async with session.get(
                f"{self._base_url}{request_path}", headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                data = await read_okx_response(response, context="get_order_status")
        entries = data.get("data", [])
        return _parse_order_status(entries[0]) if entries else None

    async def get_order_fills(self, symbol: str, order_id: str) -> list[dict]:
        """GET /api/v5/trade/fills — SIGNED. Real per-fill price/qty/fee,
        the authoritative source for realized-cost accounting, exactly
        like BinanceLiveTradeClient.get_order_trades. Returns the raw
        fill entries (fillPx, fillSz, fee, feeCcy, ts, tradeId, side)."""
        inst_id = to_okx_symbol(symbol)
        query = f"instId={inst_id}&ordId={order_id}"
        request_path = f"/api/v5/trade/fills?{query}"
        headers = self._signed_headers("GET", request_path)
        async with new_okx_http_session() as session:
            async with session.get(
                f"{self._base_url}{request_path}", headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                data = await read_okx_response(response, context="get_order_fills")
        return data.get("data", [])

    async def get_open_orders(self, symbol: str | None = None) -> list[OkxOrderStatus]:
        """GET /api/v5/trade/orders-pending — SIGNED, read-only. Startup-
        safety check, matching BinanceLiveTradeClient.get_open_orders /
        BybitLiveTradeClient.get_open_orders exactly: any order found
        here at process start is anomalous under this system's own
        design and must trigger the same pre-flight HUMAN_REVIEW_REQUIRED
        path, not a silent resume."""
        query = "instType=SPOT" + (f"&instId={to_okx_symbol(symbol)}" if symbol else "")
        request_path = f"/api/v5/trade/orders-pending?{query}"
        headers = self._signed_headers("GET", request_path)
        async with new_okx_http_session() as session:
            async with session.get(
                f"{self._base_url}{request_path}", headers=headers, timeout=aiohttp.ClientTimeout(total=self._timeout_seconds),
            ) as response:
                data = await read_okx_response(response, context="get_open_orders")
        return [_parse_order_status(entry) for entry in data.get("data", [])]
