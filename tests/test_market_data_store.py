import asyncio
import time

import pytest

from app.config.constants import MarketType
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore


def make_quote(exchange: str = "binance", symbol: str = "BTC/USDT", bid: float = 100.0, ask: float = 100.1) -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange=exchange, market=MarketType.SPOT, symbol=symbol,
        bid=bid, ask=ask, bid_quantity=1.0, ask_quantity=1.0,
        exchange_timestamp=now, received_at=now,
    )


def test_recent_volatility_pct_none_with_too_little_history():
    store = MarketDataStore()
    store.update_quote(make_quote())
    assert store.recent_volatility_pct("binance", "BTC/USDT") is None


def test_recent_volatility_pct_zero_for_constant_price():
    store = MarketDataStore()
    for _ in range(5):
        store.update_quote(make_quote(bid=100.0, ask=100.0))
    assert store.recent_volatility_pct("binance", "BTC/USDT") == pytest.approx(0.0)


def test_recent_volatility_pct_positive_for_moving_price():
    store = MarketDataStore()
    for price in [100.0, 101.0, 99.0, 102.0, 98.0]:
        store.update_quote(make_quote(bid=price, ask=price))
    assert store.recent_volatility_pct("binance", "BTC/USDT") > 0


@pytest.mark.asyncio
async def test_wait_for_update_wakes_up_on_new_quote():
    store = MarketDataStore()

    async def update_soon():
        await asyncio.sleep(0.05)
        store.update_quote(make_quote())

    task = asyncio.create_task(update_soon())
    start = time.monotonic()
    woke_on_update = await store.wait_for_update(timeout=2.0)
    elapsed = time.monotonic() - start

    await task
    assert woke_on_update is True
    assert elapsed < 1.0  # woke immediately on the update, not the 2s timeout


@pytest.mark.asyncio
async def test_wait_for_update_times_out_with_no_activity():
    store = MarketDataStore()
    woke_on_update = await store.wait_for_update(timeout=0.05)
    assert woke_on_update is False


@pytest.mark.asyncio
async def test_wait_for_update_clears_between_calls():
    store = MarketDataStore()
    store.update_quote(make_quote())

    first = await store.wait_for_update(timeout=2.0)
    assert first is True

    # No new update since the first call cleared the flag — should time out now.
    second = await store.wait_for_update(timeout=0.05)
    assert second is False
