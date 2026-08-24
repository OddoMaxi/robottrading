from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.execution.binance_live_trade_client import BinanceLiveTradeClient, BinanceTrade, _parse_order_result, aggregate_binance_trades

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


# ---- aggregate_binance_trades (pure) — item 2, user directive, 2026-08-24 --
#
# get_order_status (GET /api/v3/order) never returns fills at all — this
# is the real, authoritative fee/qty source, fetched via a separate call
# to GET /api/v3/myTrades once an order is confirmed terminal and filled.


def _trade(qty, price, commission, commission_asset, trade_id=1):
    return BinanceTrade(trade_id=trade_id, order_id=999, price=price, qty=qty, quote_qty=qty * price, commission=commission, commission_asset=commission_asset)


def test_aggregate_single_trade():
    agg = aggregate_binance_trades([_trade(3003.5, 0.00332, 3.0035, "LUNC")])
    assert agg.gross_base_qty == 3003.5
    assert agg.gross_quote == pytest.approx(3003.5 * 0.00332)
    assert agg.actual_effective_price == pytest.approx(0.00332)
    assert agg.fees_by_asset == {"LUNC": 3.0035}


def test_aggregate_multiple_trades_same_asset_sums_them():
    """A single market order can match against several price levels —
    each becomes its own trade record, all sharing the same orderId."""
    agg = aggregate_binance_trades([_trade(1000.0, 0.00332, 1.0, "LUNC", trade_id=1), _trade(2003.5, 0.00333, 2.0035, "LUNC", trade_id=2)])
    assert agg.gross_base_qty == pytest.approx(3003.5)
    assert agg.fees_by_asset == {"LUNC": pytest.approx(3.0035)}
    assert agg.actual_effective_price == pytest.approx(agg.gross_quote / 3003.5)


def test_aggregate_multiple_trades_different_assets_kept_separate():
    agg = aggregate_binance_trades([_trade(1000.0, 0.00332, 1.0, "LUNC", trade_id=1), _trade(2003.5, 0.00333, 0.02, "USDT", trade_id=2)])
    assert agg.fees_by_asset == {"LUNC": 1.0, "USDT": 0.02}


def test_aggregate_empty_trades_list():
    agg = aggregate_binance_trades([])
    assert agg.gross_base_qty == 0.0
    assert agg.gross_quote == 0.0
    assert agg.actual_effective_price is None
    assert agg.fees_by_asset == {}


# ---- get_order_trades (real network call, mocked) -------------------------


class _FakeMyTradesResponse:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> list[dict]:
        return self._payload

    async def __aenter__(self) -> "_FakeMyTradesResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeMyTradesSession:
    def __init__(self, payload: list[dict], captured: dict) -> None:
        self._payload = payload
        self._captured = captured

    def get(self, url: str, headers=None, params=None, timeout=None):
        self._captured["url"] = url
        self._captured["params"] = params
        return _FakeMyTradesResponse(self._payload)

    async def __aenter__(self) -> "_FakeMyTradesSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_settings():
    return SimpleNamespace(binance_api_key="test-key", binance_api_secret="test-secret")


async def test_get_order_trades_hits_my_trades_endpoint_and_parses_the_response(monkeypatch):
    payload = [
        {"id": 1, "orderId": 999, "price": "0.00332", "qty": "3003.5", "quoteQty": "9.9716", "commission": "3.0035", "commissionAsset": "LUNC"},
    ]
    captured: dict = {}
    monkeypatch.setattr("app.execution.binance_live_trade_client.get_settings", _fake_settings)
    client = BinanceLiveTradeClient()
    with patch("app.execution.binance_live_trade_client.aiohttp.ClientSession", return_value=_FakeMyTradesSession(payload, captured)):
        trades = await client.get_order_trades("LUNCUSDT", 999)
    assert "/api/v3/myTrades" in captured["url"]
    assert captured["params"]["orderId"] == 999
    assert len(trades) == 1
    assert trades[0].qty == 3003.5
    assert trades[0].commission == 3.0035
    assert trades[0].commission_asset == "LUNC"


async def test_get_order_trades_empty_when_no_trades_found(monkeypatch):
    monkeypatch.setattr("app.execution.binance_live_trade_client.get_settings", _fake_settings)
    client = BinanceLiveTradeClient()
    with patch("app.execution.binance_live_trade_client.aiohttp.ClientSession", return_value=_FakeMyTradesSession([], {})):
        trades = await client.get_order_trades("LUNCUSDT", 999)
    assert trades == []
