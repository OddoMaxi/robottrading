import time

import pytest

from app.config.constants import MarketType
from app.engines.basis import BasisArbitrageEngine
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import DeliveryFuturesSnapshot, MarketDataStore


def make_spot_quote(bid: float, ask: float, qty: float = 10.0) -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange="binance", market=MarketType.SPOT, symbol="BTC/USDT",
        bid=bid, ask=ask, bid_quantity=qty, ask_quantity=qty,
        exchange_timestamp=now, received_at=now,
    )


def make_future(price: float, days_to_expiry: float) -> DeliveryFuturesSnapshot:
    now = time.time()
    return DeliveryFuturesSnapshot(
        exchange="binance", symbol="BTC/USDT", contract_symbol="BTCUSDT_260327",
        price=price, delivery_time=now + days_to_expiry * 86400, received_at=now,
    )


@pytest.mark.asyncio
async def test_positive_basis_detected_with_annualized_yield():
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))
    store.update_delivery_future(make_future(price=101_000, days_to_expiry=90))

    engine = BasisArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.gross_spread_pct == pytest.approx(1.0, abs=1e-6)  # (101000-100000)/100000 * 100
    # Annualized: 1% basis over 90 days -> roughly 4x over a year
    assert opp.annualized_pct == pytest.approx(1.0 * 365 / 90, rel=1e-6)
    assert opp.days_to_expiry == pytest.approx(90, abs=0.1)
    assert opp.net_spread_pct < opp.gross_spread_pct  # fees reduce it
    assert opp.legs[1]["symbol"] == "BTCUSDT_260327"


@pytest.mark.asyncio
async def test_negative_basis_backwardation_not_traded():
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))
    store.update_delivery_future(make_future(price=99_000, days_to_expiry=90))  # future below spot

    engine = BasisArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    assert await engine.detect() == []


@pytest.mark.asyncio
async def test_no_future_data_yields_nothing():
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))

    engine = BasisArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    assert await engine.detect() == []
