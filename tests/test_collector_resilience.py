"""Failure injection (Reality Engine spec, section 78) — WS disconnect
scenarios for MarketDataCollector.run()'s reconnect/backoff wrapper, which
had zero test coverage despite being the thing standing between "one bad
tick" and "this exchange's data silently goes dark for good".
"""

import asyncio
from unittest.mock import patch

import pytest

from app.collectors.base import MarketDataCollector
from app.market_data.store import MarketDataStore


class _FlakyCollector(MarketDataCollector):
    exchange = "test-exchange"

    def __init__(self, symbols: list[str], fail_times: int) -> None:
        super().__init__(symbols)
        self._fail_times = fail_times
        self.attempts = 0

    async def _run_once(self, store: MarketDataStore) -> None:
        self.attempts += 1
        if self.attempts <= self._fail_times:
            raise ConnectionError("simulated WS disconnect")
        raise asyncio.CancelledError  # stops the otherwise-infinite loop once healthy


@pytest.mark.asyncio
async def test_run_reconnects_after_a_websocket_disconnect():
    collector = _FlakyCollector(["BTC/USDT"], fail_times=1)
    store = MarketDataStore()
    with patch("app.collectors.base.asyncio.sleep", return_value=None) as mock_sleep:
        with pytest.raises(asyncio.CancelledError):
            await collector.run(store)
    assert collector.attempts == 2  # one disconnect, one successful retry
    mock_sleep.assert_called_once()


@pytest.mark.asyncio
async def test_backoff_doubles_on_repeated_failures_and_is_capped_at_30s():
    collector = _FlakyCollector(["BTC/USDT"], fail_times=6)
    store = MarketDataStore()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with patch("app.collectors.base.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await collector.run(store)

    assert sleeps == [1.0, 2.0, 4.0, 8.0, 16.0, 30.0]


@pytest.mark.asyncio
async def test_backoff_resets_after_a_clean_run_before_the_next_failure():
    """A connection that ran cleanly for a while and then drops shouldn't
    inherit whatever backoff had built up from a much earlier, unrelated
    failure streak."""

    class _CleanThenFlaky(MarketDataCollector):
        exchange = "test-exchange"

        def __init__(self, symbols: list[str]) -> None:
            super().__init__(symbols)
            self.call_count = 0

        async def _run_once(self, store: MarketDataStore) -> None:
            self.call_count += 1
            if self.call_count == 1:
                raise ConnectionError("first failure")
            if self.call_count == 2:
                return  # clean disconnect — no exception, resets backoff to 1.0
            if self.call_count == 3:
                raise ConnectionError("second failure — should start from backoff=1.0 again")
            raise asyncio.CancelledError

    collector = _CleanThenFlaky(["BTC/USDT"])
    store = MarketDataStore()
    sleeps = []

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    with patch("app.collectors.base.asyncio.sleep", side_effect=fake_sleep):
        with pytest.raises(asyncio.CancelledError):
            await collector.run(store)

    assert sleeps == [1.0, 1.0]


@pytest.mark.asyncio
async def test_cancelled_error_propagates_without_being_treated_as_a_crash():
    class _ImmediatelyCancelled(MarketDataCollector):
        exchange = "test-exchange"

        async def _run_once(self, store: MarketDataStore) -> None:
            raise asyncio.CancelledError

    collector = _ImmediatelyCancelled(["BTC/USDT"])
    store = MarketDataStore()
    with pytest.raises(asyncio.CancelledError):
        await collector.run(store)
