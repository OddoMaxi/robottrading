import pytest

from app.execution.binance_live_trade_client import _parse_order_result

FILLED_ORDER_FIXTURE = {
    "symbol": "LUNCUSDT",
    "orderId": 123456,
    "clientOrderId": "test-order-1",
    "status": "FILLED",
    "executedQty": "183150.00000000",
    "cummulativeQuoteQty": "10.00000000",
    "fills": [
        {"price": "0.00005460", "qty": "183150.00000000", "commission": "0.01000000", "commissionAsset": "USDT"},
    ],
}

PARTIAL_ORDER_FIXTURE = {
    "symbol": "LUNCUSDT",
    "orderId": 123457,
    "clientOrderId": "test-order-2",
    "status": "PARTIALLY_FILLED",
    "executedQty": "90000.00000000",
    "cummulativeQuoteQty": "4.90000000",
    "fills": [
        {"price": "0.00005444", "qty": "90000.00000000", "commission": "0.00490000", "commissionAsset": "USDT"},
    ],
}

NEW_ORDER_FIXTURE = {
    "symbol": "LUNCUSDT",
    "orderId": 123458,
    "clientOrderId": "test-order-3",
    "status": "NEW",
    "executedQty": "0.00000000",
    "cummulativeQuoteQty": "0.00000000",
    "fills": [],
}


def test_parse_filled_order():
    result = _parse_order_result(FILLED_ORDER_FIXTURE)
    assert result.is_filled is True
    assert result.is_terminal is True
    assert result.executed_qty == 183150.0
    assert result.average_fill_price() == pytest.approx(10.0 / 183150.0)
    assert result.total_fees_by_asset() == {"USDT": 0.01}


def test_parse_partially_filled_order_is_not_treated_as_terminal():
    """Conservative by design: PARTIALLY_FILLED alone doesn't guarantee
    Binance is done with this order — callers must keep polling until a
    genuinely terminal status or their own strict timeout, never assume
    a partial fill is the final word."""
    result = _parse_order_result(PARTIAL_ORDER_FIXTURE)
    assert result.is_filled is False
    assert result.is_partially_filled is True
    assert result.is_terminal is False


def test_parse_new_order_is_not_terminal():
    result = _parse_order_result(NEW_ORDER_FIXTURE)
    assert result.is_terminal is False
    assert result.average_fill_price() is None


def test_average_fill_price_none_when_nothing_executed():
    result = _parse_order_result(NEW_ORDER_FIXTURE)
    assert result.average_fill_price() is None
