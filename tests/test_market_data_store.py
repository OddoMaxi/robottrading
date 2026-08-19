import asyncio
import time

import pytest

from app.config.constants import MarketType
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore


def make_quote(exchange: str = "binance", symbol: str = "BTC/USDT") -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange=exchange, market=MarketType.SPOT, symbol=symbol,
        bid=100.0, ask=100.1, bid_quantity=1.0, ask_quantity=1.0,
        exchange_timestamp=now, received_at=now,
    )


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
