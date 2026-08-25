import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.execution.okx_live_trade_client import (
    OkxLiveCredentialsMissing,
    OkxLiveTradeClient,
    _new_client_order_id,
    _parse_order_ack,
    _parse_order_status,
)


def test_new_client_order_id_is_alphanumeric_and_at_most_32_chars():
    coid = _new_client_order_id("okx")
    assert len(coid) <= 32
    assert coid.isalnum()
    assert coid.startswith("okx")


ACK_FIXTURE = {"code": "0", "data": [{"ordId": "12345", "clOrdId": "okxabc123", "sCode": "0", "sMsg": ""}]}
ACK_REJECTED_FIXTURE = {"code": "1", "data": [{"ordId": "", "clOrdId": "okxabc123", "sCode": "51008", "sMsg": "Order failed. Insufficient balance"}]}

FILLED_STATUS_FIXTURE = {
    "data": [{
        "ordId": "12345", "clOrdId": "okxabc123", "instId": "RVN-USDT", "side": "sell", "state": "filled",
        "accFillSz": "2130.9", "avgPx": "0.003415", "fee": "-0.0072770235", "feeCcy": "USDT",
    }]
}

FEE_IN_BASE_ASSET_FIXTURE = {
    "data": [{
        "ordId": "999", "clOrdId": "okxbuy1", "instId": "RVN-USDT", "side": "buy", "state": "filled",
        "accFillSz": "2130.9669", "avgPx": "0.00331", "fee": "-2.1331", "feeCcy": "RVN",
    }]
}


def test_parse_order_ack_success():
    ack = _parse_order_ack(ACK_FIXTURE)
    assert ack.accepted is True
    assert ack.order_id == "12345"
    assert ack.client_order_id == "okxabc123"


def test_parse_order_ack_rejection():
    ack = _parse_order_ack(ACK_REJECTED_FIXTURE)
    assert ack.accepted is False
    assert ack.status_code == "51008"
    assert "Insufficient balance" in ack.status_message


def test_parse_order_status_filled_fee_in_quote_asset():
    status = _parse_order_status(FILLED_STATUS_FIXTURE["data"][0])
    assert status.is_filled is True
    assert status.filled_qty == pytest.approx(2130.9)
    assert status.avg_fill_price == pytest.approx(0.003415)
    assert status.fee_amount == pytest.approx(0.0072770235)  # abs() of the negative OKX convention
    assert status.fee_asset == "USDT"


def test_parse_order_status_fee_in_base_asset_never_confused_with_quote():
    status = _parse_order_status(FEE_IN_BASE_ASSET_FIXTURE["data"][0])
    assert status.fee_asset == "RVN"
    assert status.fee_amount == pytest.approx(2.1331)


def test_place_market_order_rejects_invalid_side():
    client = OkxLiveTradeClient()
    with pytest.raises(ValueError):
        import asyncio
        asyncio.run(client.place_market_order("RVN/USDT", "hold", 100.0))


def test_get_order_status_requires_exactly_one_identifier():
    client = OkxLiveTradeClient()
    import asyncio

    async def _both():
        return await client.get_order_status("RVN/USDT", order_id="1", client_order_id="c1")

    async def _neither():
        return await client.get_order_status("RVN/USDT")

    with pytest.raises(ValueError):
        asyncio.run(_both())
    with pytest.raises(ValueError):
        asyncio.run(_neither())


class _FakeResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> dict:
        return self._payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, payload: dict, captured: dict) -> None:
        self._payload = payload
        self._captured = captured

    def get(self, url: str, headers=None, timeout=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        return _FakeResponse(self._payload)

    def post(self, url: str, headers=None, data=None, timeout=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["body"] = json.loads(data)
        return _FakeResponse(self._payload)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_settings():
    return SimpleNamespace(okx_api_key="test-key", okx_api_secret="test-secret", okx_api_passphrase="test-pass")


async def test_place_market_order_sends_base_ccy_sizing_and_cash_mode(monkeypatch):
    """quantity must always mean the BASE asset amount, matching every
    other live-trade client in this codebase -- tgtCcy=base_ccy is set
    explicitly so this holds for both buy and sell (OKX's own default
    differs by side otherwise). tdMode=cash is the hard "no leverage"
    requirement, structurally enforced in the request body itself."""
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_live_trade_client.get_settings", _fake_settings)
    client = OkxLiveTradeClient()
    with patch("app.execution.okx_live_trade_client.aiohttp.ClientSession", return_value=_FakeSession(ACK_FIXTURE, captured)):
        ack = await client.place_market_order("RVN/USDT", "sell", 2130.9, client_order_id="okxabc123")
    assert ack.accepted is True
    assert captured["body"]["instId"] == "RVN-USDT"
    assert captured["body"]["tdMode"] == "cash"
    assert captured["body"]["ordType"] == "market"
    assert captured["body"]["tgtCcy"] == "base_ccy"
    assert captured["body"]["sz"] == "2130.9"
    assert captured["body"]["clOrdId"] == "okxabc123"


async def test_get_order_status_by_order_id(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_live_trade_client.get_settings", _fake_settings)
    client = OkxLiveTradeClient()
    with patch("app.execution.okx_live_trade_client.aiohttp.ClientSession", return_value=_FakeSession(FILLED_STATUS_FIXTURE, captured)):
        status = await client.get_order_status("RVN/USDT", order_id="12345")
    assert status.is_filled is True
    assert "ordId=12345" in captured["url"]
    assert "instId=RVN-USDT" in captured["url"]


async def test_get_order_status_returns_none_when_no_data(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_live_trade_client.get_settings", _fake_settings)
    client = OkxLiveTradeClient()
    with patch("app.execution.okx_live_trade_client.aiohttp.ClientSession", return_value=_FakeSession({"data": []}, captured)):
        status = await client.get_order_status("RVN/USDT", order_id="nonexistent")
    assert status is None


async def test_get_open_orders_scopes_to_spot_inst_type(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_live_trade_client.get_settings", _fake_settings)
    client = OkxLiveTradeClient()
    with patch("app.execution.okx_live_trade_client.aiohttp.ClientSession", return_value=_FakeSession({"data": [FILLED_STATUS_FIXTURE["data"][0]]}, captured)):
        orders = await client.get_open_orders()
    assert "instType=SPOT" in captured["url"]
    assert len(orders) == 1


async def test_get_order_fills_returns_raw_entries(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_live_trade_client.get_settings", _fake_settings)
    client = OkxLiveTradeClient()
    fills_payload = {"data": [{"fillPx": "0.003415", "fillSz": "2130.9", "fee": "-0.0073", "feeCcy": "USDT", "tradeId": "t1"}]}
    with patch("app.execution.okx_live_trade_client.aiohttp.ClientSession", return_value=_FakeSession(fills_payload, captured)):
        fills = await client.get_order_fills("RVN/USDT", "12345")
    assert len(fills) == 1
    assert fills[0]["tradeId"] == "t1"


def test_signed_headers_raise_without_credentials(monkeypatch):
    client = OkxLiveTradeClient()
    monkeypatch.setattr("app.execution.okx_live_trade_client.get_settings", lambda: SimpleNamespace(okx_api_key="", okx_api_secret="", okx_api_passphrase=""))
    with pytest.raises(OkxLiveCredentialsMissing):
        client._signed_headers("GET", "/api/v5/trade/order")
