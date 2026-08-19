import time

import pytest

from app.config.constants import MarketType
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.engines.triangular import TriangularArbitrageEngine
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore


def make_quote(exchange: str, symbol: str, bid: float, ask: float, qty: float = 10.0) -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange=exchange,
        market=MarketType.SPOT,
        symbol=symbol,
        bid=bid,
        ask=ask,
        bid_quantity=qty,
        ask_quantity=qty,
        exchange_timestamp=now,
        received_at=now,
    )


@pytest.mark.asyncio
async def test_cross_exchange_detects_spread():
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000))
    store.update_quote(make_quote("okx", "BTC/USDT", bid=100_300, ask=100_310))

    engine = CrossExchangeArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.legs[0] == {"exchange": "binance", "side": "buy", "market": "spot", "price": pytest.approx(100_000), "quantity": pytest.approx(0.01)}
    assert opp.legs[1]["exchange"] == "okx"
    assert opp.gross_spread_pct > 0
    assert opp.net_spread_pct < opp.gross_spread_pct  # fees reduce it


@pytest.mark.asyncio
async def test_cross_exchange_no_opportunity_without_spread():
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=100_000, ask=100_010))
    store.update_quote(make_quote("okx", "BTC/USDT", bid=100_005, ask=100_015))

    engine = CrossExchangeArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    assert await engine.detect() == []


@pytest.mark.asyncio
async def test_triangular_detects_loop():
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000))
    store.update_quote(make_quote("binance", "ETH/BTC", bid=0.0359, ask=0.0360))
    # 100_000 * 0.0360 = 3_600 implied ETH/USDT via the BTC route — 3_650 direct is richer.
    store.update_quote(make_quote("binance", "ETH/USDT", bid=3_650, ask=3_651))

    engine = TriangularArbitrageEngine(exchange="binance", store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.symbol == "USDT->BTC->ETH->USDT"
    assert opp.gross_spread_pct == pytest.approx(1.3889, abs=1e-3)


@pytest.mark.asyncio
async def test_triangular_no_opportunity_when_consistent():
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000))
    store.update_quote(make_quote("binance", "ETH/BTC", bid=0.0359, ask=0.0360))
    store.update_quote(make_quote("binance", "ETH/USDT", bid=3_600, ask=3_601))

    engine = TriangularArbitrageEngine(exchange="binance", store=store, capital_usd=1_000)
    assert await engine.detect() == []
