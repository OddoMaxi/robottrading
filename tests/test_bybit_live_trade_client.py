from app.execution.bybit_live_trade_client import _parse_order_ack, _parse_order_status

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


def test_parse_new_order_status_is_not_terminal():
    status = _parse_order_status(NEW_STATUS_FIXTURE)
    assert status is not None
    assert status.is_terminal is False
    assert status.avg_price is None  # empty string must not become 0.0 — that would look like a real price


def test_parse_order_status_returns_none_when_order_not_found():
    """An order that already rolled off this endpoint must return None,
    never a fabricated status — the caller falls back to order history."""
    assert _parse_order_status(EMPTY_STATUS_FIXTURE) is None
