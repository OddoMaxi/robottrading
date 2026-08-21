"""Failure injection (Reality Engine spec, section 78) — API timeout
scenario for the REST-polled funding collectors. poll_binance_funding is
the representative case; poll_okx_funding/poll_bybit_funding/
poll_binance_delivery_futures share the identical try/except-around-the-
request-then-sleep-and-retry shape.
"""

import asyncio
from unittest.mock import patch

import pytest

from app.collectors.binance.funding import poll_binance_funding
from app.market_data.store import MarketDataStore


class _FakeResponse:
    def __init__(self, payload: list[dict]) -> None:
        self._payload = payload

    async def json(self) -> list[dict]:
        return self._payload

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, responses: list) -> None:
        self._responses = iter(responses)

    def get(self, url: str, timeout=None):
        item = next(self._responses)
        if isinstance(item, BaseException):
            raise item
        return _FakeResponse(item)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


@pytest.mark.asyncio
async def test_poll_binance_funding_survives_an_api_timeout_and_recovers_next_cycle():
    good_payload = [
        {"symbol": "BTCUSDT", "lastFundingRate": "0.0001", "nextFundingTime": 1_000_000_000.0, "markPrice": "50000", "indexPrice": "50000"}
    ]
    responses = [TimeoutError("simulated API timeout"), good_payload, asyncio.CancelledError()]
    fake_session = _FakeSession(responses)
    store = MarketDataStore()

    with (
        patch("app.collectors.binance.funding.aiohttp.ClientSession", return_value=fake_session),
        patch("app.collectors.binance.funding.asyncio.sleep", return_value=None),
    ):
        with pytest.raises(asyncio.CancelledError):
            await poll_binance_funding(store, ["BTC"], interval_seconds=0.0)

    snapshot = store.funding_for_symbol("BTC/USDT").get("binance")
    assert snapshot is not None, "the timeout should not have prevented the next cycle's successful update"
    assert snapshot.funding_rate == pytest.approx(0.0001)
