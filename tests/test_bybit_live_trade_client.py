import json
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.execution.bybit_live_trade_client import BybitLiveTradeClient, _parse_order_ack, _parse_order_status

ACK_FIXTURE = {"retCode": 0, "retMsg": "OK", "result": {"orderId": "abc123", "orderLinkId": "my-link-1"}}

FILLED_STATUS_FIXTURE = {
    "result": {
        "list": [
            {
                "orderId": "abc123",
                "orderLinkId": "my-link-1",
                "symbol": "LUNCUSDT",
                "side": "Sell",
                "orderStatus": "Filled",
                "cumExecQty": "183150",
                "cumExecValue": "10.05",
                "cumExecFee": "0.01005",
                "avgPrice": "0.0000549",
            }
        ]
    }
}

FILLED_STATUS_FEE_IN_BASE_ASSET_FIXTURE = {
    "result": {
        "list": [
            {
                "orderId": "order-rvn-1",
                "orderLinkId": "link-rvn-1",
                "symbol": "RVNUSDT",
                "side": "Buy",
                "orderStatus": "Filled",
                "cumExecQty": "2917.9",
                "cumExecValue": "10.0",
                "cumExecFee": "2.9179",  # deprecated, currency-less — must NOT be trusted as USDT
                "avgPrice": "0.003427",
                "cumFeeDetail": {"RVN": "2.9179"},
            }
        ]
    }
}

FILLED_STATUS_FEE_IN_QUOTE_ASSET_FIXTURE = {
    "result": {
        "list": [
            {
                "orderId": "order-rvn-2",
                "orderLinkId": "link-rvn-2",
                "symbol": "RVNUSDT",
                "side": "Sell",
                "orderStatus": "Filled",
                "cumExecQty": "2914.9821",
                "cumExecValue": "9.99",
                "cumExecFee": "0.01",
                "avgPrice": "0.003429",
                "cumFeeDetail": {"USDT": "0.01"},
            }
        ]
    }
}

NEW_STATUS_FIXTURE = {
    "result": {
        "list": [
            {
                "orderId": "abc124",
                "orderLinkId": "my-link-2",
                "symbol": "LUNCUSDT",
                "side": "Sell",
                "orderStatus": "New",
                "cumExecQty": "0",
                "cumExecValue": "0",
                "cumExecFee": "0",
                "avgPrice": "",
            }
        ]
    }
}

EMPTY_STATUS_FIXTURE = {"result": {"list": []}}


def test_parse_order_ack():
    ack = _parse_order_ack(ACK_FIXTURE)
    assert ack.order_id == "abc123"
    assert ack.order_link_id == "my-link-1"


def test_parse_filled_order_status():
    status = _parse_order_status(FILLED_STATUS_FIXTURE)
    assert status is not None
    assert status.is_filled is True
    assert status.is_terminal is True
    assert status.cum_exec_qty == 183150.0
    assert status.avg_price == 0.0000549


def test_parse_filled_order_status_without_cum_fee_detail_defaults_to_empty():
    """A response shape without cumFeeDetail at all (older/partial
    payload) must not crash — total_fees_by_asset() returns {}, never a
    guess."""
    status = _parse_order_status(FILLED_STATUS_FIXTURE)
    assert status.total_fees_by_asset() == {}


def test_parse_order_status_captures_fee_charged_in_the_base_asset():
    """Regression (2026-08-24): the first real Bybit fill charged its
    fee in RVN (the base asset), which cum_exec_fee alone cannot reveal
    — cumFeeDetail is the only currency-aware source."""
    status = _parse_order_status(FILLED_STATUS_FEE_IN_BASE_ASSET_FIXTURE)
    assert status.total_fees_by_asset() == {"RVN": 2.9179}
    assert status.cum_exec_fee == 2.9179  # still parsed for raw/debug visibility, just never trusted alone


def test_parse_order_status_captures_fee_charged_in_the_quote_asset():
    status = _parse_order_status(FILLED_STATUS_FEE_IN_QUOTE_ASSET_FIXTURE)
    assert status.total_fees_by_asset() == {"USDT": 0.01}


def test_parse_new_order_status_is_not_terminal():
    status = _parse_order_status(NEW_STATUS_FIXTURE)
    assert status is not None
    assert status.is_terminal is False
    assert status.avg_price is None  # empty string must not become 0.0 — that would look like a real price


def test_parse_order_status_returns_none_when_order_not_found():
    """An order that already rolled off this endpoint must return None,
    never a fabricated status — the caller falls back to order history."""
    assert _parse_order_status(EMPTY_STATUS_FIXTURE) is None


# ---- place_market_order payload construction (2026-08-24 fix) ------------
#
# A real order was rejected by Bybit with retCode=170003 "An unknown
# parameter was sent". Verified against the current Bybit v5 docs: qty's
# meaning now depends on side (marketUnit="quoteCoin" for Buy, qty=USDT;
# marketUnit="baseCoin" for Sell, qty=asset quantity), plus explicit
# isLeverage=0 and orderFilter="Order". These tests exercise the exact
# request body constructed, never a real network call.

EXPECTED_KEYS = {"category", "symbol", "side", "orderType", "qty", "marketUnit", "isLeverage", "orderFilter", "orderLinkId"}
FORBIDDEN_KEYS = {
    "price", "triggerPrice", "triggerDirection", "triggerBy", "positionIdx",
    "reduceOnly", "closeOnTrigger", "orderIv", "mmp", "tpslMode", "bboSideType",
    "bboLevel", "timeInForce", "takeProfit", "stopLoss", "smpType",
}


class _FakeCreateOrderResponse:
    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> dict:
        return self._payload

    async def __aenter__(self) -> "_FakeCreateOrderResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeCreateOrderSession:
    def __init__(self, payload: dict, captured: dict) -> None:
        self._payload = payload
        self._captured = captured

    def post(self, url: str, headers=None, data=None, timeout=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        self._captured["body"] = json.loads(data)
        return _FakeCreateOrderResponse(self._payload)

    async def __aenter__(self) -> "_FakeCreateOrderSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_settings():
    return SimpleNamespace(bybit_api_key="test-key", bybit_api_secret="test-secret")


ACK_SUCCESS = {"retCode": 0, "retMsg": "OK", "result": {"orderId": "order-1", "orderLinkId": "link-1"}}


async def _place_and_capture(monkeypatch, side: str, qty: float, order_link_id: str = "link-1", payload: dict = ACK_SUCCESS):
    captured: dict = {}
    monkeypatch.setattr("app.execution.bybit_live_trade_client.get_settings", _fake_settings)
    client = BybitLiveTradeClient()
    with patch("app.execution.bybit_live_trade_client.aiohttp.ClientSession", return_value=_FakeCreateOrderSession(payload, captured)):
        result = await client.place_market_order("RVNUSDT", side, qty=qty, order_link_id=order_link_id)
    return result, captured


async def test_spot_market_buy_uses_quote_coin_unit_and_usdt_qty(monkeypatch):
    """Item 1: BUY sized in USDT notional (marketUnit=quoteCoin)."""
    _, captured = await _place_and_capture(monkeypatch, "Buy", qty=10.0)
    assert captured["body"]["side"] == "Buy"
    assert captured["body"]["marketUnit"] == "quoteCoin"
    assert captured["body"]["qty"] == "10"


async def test_spot_market_sell_uses_base_coin_unit_and_asset_qty(monkeypatch):
    """Item 2: SELL sized in base-asset quantity (marketUnit=baseCoin) —
    unchanged meaning from before the fix."""
    _, captured = await _place_and_capture(monkeypatch, "Sell", qty=41.5)
    assert captured["body"]["side"] == "Sell"
    assert captured["body"]["marketUnit"] == "baseCoin"
    assert captured["body"]["qty"] == "41.5"


async def test_exact_payload_sent_for_a_buy_order(monkeypatch):
    """Item 3: the exact payload, not just individual fields."""
    _, captured = await _place_and_capture(monkeypatch, "Buy", qty=10.0, order_link_id="inventory-abc")
    assert captured["body"] == {
        "category": "spot",
        "symbol": "RVNUSDT",
        "side": "Buy",
        "orderType": "Market",
        "qty": "10",
        "marketUnit": "quoteCoin",
        "isLeverage": 0,
        "orderFilter": "Order",
        "orderLinkId": "inventory-abc",
    }
    assert captured["headers"]["Content-Type"] == "application/json"


async def test_exact_payload_sent_for_a_sell_order(monkeypatch):
    _, captured = await _place_and_capture(monkeypatch, "Sell", qty=41.5, order_link_id="arb-xyz")
    assert captured["body"] == {
        "category": "spot",
        "symbol": "RVNUSDT",
        "side": "Sell",
        "orderType": "Market",
        "qty": "41.5",
        "marketUnit": "baseCoin",
        "isLeverage": 0,
        "orderFilter": "Order",
        "orderLinkId": "arb-xyz",
    }


async def test_no_forbidden_or_unsupported_parameters_are_ever_sent(monkeypatch):
    """Item 4: the body's key set is EXACTLY the documented, relevant
    fields for a plain spot market order — nothing from the
    conditional/TP-SL/leverage/options families Bybit's docs list as not
    applicable here."""
    for side, qty in (("Buy", 10.0), ("Sell", 41.5)):
        _, captured = await _place_and_capture(monkeypatch, side, qty=qty)
        assert set(captured["body"].keys()) == EXPECTED_KEYS
        assert set(captured["body"].keys()).isdisjoint(FORBIDDEN_KEYS)


async def test_invalid_side_is_rejected_before_any_network_call(monkeypatch):
    monkeypatch.setattr("app.execution.bybit_live_trade_client.get_settings", _fake_settings)
    client = BybitLiveTradeClient()
    with patch("app.execution.bybit_live_trade_client.aiohttp.ClientSession") as mock_session_cls:
        with pytest.raises(ValueError):
            await client.place_market_order("RVNUSDT", "Hold", qty=10.0, order_link_id="link-1")
        mock_session_cls.assert_not_called()


# ---- Item 5: parsing Bybit's return -----------------------------------


async def test_successful_create_order_response_parses_into_an_ack(monkeypatch):
    result, _ = await _place_and_capture(monkeypatch, "Buy", qty=10.0, payload=ACK_SUCCESS)
    assert result.order_id == "order-1"
    assert result.order_link_id == "link-1"


async def test_rejected_create_order_response_raises_with_full_detail(monkeypatch):
    """The earlier version of this error only kept retCode/retMsg,
    discarding the rest of Bybit's response — not enough detail to
    diagnose a real rejection (exactly what happened with retCode=170003).
    The raised error must now carry the full response."""
    rejected = {"retCode": 170003, "retMsg": "An unknown parameter was sent.", "result": {}, "retExtInfo": {"foo": "bar"}}
    with pytest.raises(RuntimeError) as exc_info:
        await _place_and_capture(monkeypatch, "Buy", qty=10.0, payload=rejected)
    message = str(exc_info.value)
    assert "170003" in message
    assert "An unknown parameter was sent." in message
    assert "retExtInfo" in message
