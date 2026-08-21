import time

import pytest

from app.config.constants import MarketType
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.engines.triangular import TriangularArbitrageEngine
from app.market_data.normalizer import NormalizedQuote
from app.market_data.orderbook import OrderBook, OrderBookLevel
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
async def test_cross_exchange_uses_real_order_book_depth_when_available():
    """Reality Engine spec, sections 7-8 — with only $100 available at the
    literal top-of-book price, a $1,000 trade should VWAP through deeper
    real levels (when present) instead of being capped at the thin
    top-of-book quantity the way the single-level fallback would cap it."""
    store = MarketDataStore()
    thin_top_of_book = make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000, qty=0.001)  # only $100 at best price
    store.update_quote(thin_top_of_book)
    store.update_quote(make_quote("okx", "BTC/USDT", bid=100_300, ask=100_310))
    store.update_order_book(
        OrderBook(
            exchange="binance",
            symbol="BTC/USDT",
            bids=[OrderBookLevel(99_990, 0.001)],
            asks=[OrderBookLevel(100_000, 0.001), OrderBookLevel(100_010, 0.01)],  # deeper levels absorb the rest
            timestamp=time.time(),
        )
    )

    engine = CrossExchangeArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    assert len(opportunities) == 1
    # Filled close to the full $1,000 via the deeper book, not capped at
    # the ~$100 the top-of-book quote's own ask_quantity would allow.
    assert opportunities[0].capital_usd > 500


@pytest.mark.asyncio
async def test_cross_exchange_falls_back_to_top_of_book_without_a_local_order_book():
    """Same thin top-of-book, but no order book populated for it — must
    cap the fill at what the quote itself says is available, same as
    before real depth data existed."""
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000, qty=0.001))
    store.update_quote(make_quote("okx", "BTC/USDT", bid=100_300, ask=100_310))

    engine = CrossExchangeArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    assert len(opportunities) == 1
    assert opportunities[0].capital_usd == pytest.approx(100.0, abs=1.0)


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


@pytest.mark.asyncio
async def test_triangular_fdusd_stablecoin_loop():
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "USDC/USDT", bid=0.9998, ask=0.9999, qty=100_000))
    store.update_quote(make_quote("binance", "FDUSD/USDC", bid=0.9997, ask=0.9998, qty=100_000))
    # Exaggerated on purpose (real stablecoin spreads are tiny) so the loop
    # clearly clears the 3-leg break-even floor and isn't just testing the
    # break-even gate itself.
    store.update_quote(make_quote("binance", "FDUSD/USDT", bid=1.010, ask=1.011, qty=100_000))

    engine = TriangularArbitrageEngine(exchange="binance", store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    fdusd_opps = [o for o in opportunities if o.symbol == "USDT->USDC->FDUSD->USDT"]
    assert len(fdusd_opps) == 1
    assert fdusd_opps[0].gross_spread_pct > 0
    assert fdusd_opps[0].legs[1]["symbol"] == "FDUSD/USDC"


@pytest.mark.asyncio
async def test_triangular_auto_generated_stablecoin_bridge_loop_resolves_real_directions():
    """Section 5 — a bridge loop between two *different* stablecoins via a
    crypto asset (e.g. USDC -> ETH -> USDT -> USDC) must resolve to
    buy/sell/buy in real Binance market directions (ETH/USDC, ETH/USDT,
    USDC/USDT — crypto is always the listed base, never the stablecoin),
    not the original fixed buy/buy/sell shape."""
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "ETH/USDC", bid=2_999, ask=3_000, qty=100))
    store.update_quote(make_quote("binance", "ETH/USDT", bid=3_030, ask=3_031, qty=100))
    store.update_quote(make_quote("binance", "USDC/USDT", bid=0.999, ask=1.0, qty=100_000))

    engine = TriangularArbitrageEngine(exchange="binance", store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    opp = next(o for o in opportunities if o.symbol == "USDC->ETH->USDT->USDC")
    assert [leg["side"] for leg in opp.legs] == ["buy", "sell", "buy"]
    assert [leg["symbol"] for leg in opp.legs] == ["ETH/USDC", "ETH/USDT", "USDC/USDT"]
    assert opp.gross_spread_pct == pytest.approx(1.0, abs=1e-6)


@pytest.mark.asyncio
async def test_triangular_middle_leg_fee_uses_correct_usd_notional():
    """Regression: the middle leg's fee must be computed on its real USD
    notional (spend * that asset's own USD price at that point in the
    loop), not on the raw BTC-denominated quantity — treating 0.01 BTC as
    "$0.01 notional" would understate that leg's fee by ~5 orders of
    magnitude and make triangular loops look far more profitable than they are."""
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000, qty=100))
    store.update_quote(make_quote("binance", "ETH/BTC", bid=0.0199, ask=0.02, qty=1_000))
    store.update_quote(make_quote("binance", "ETH/USDT", bid=2_020, ask=2_021, qty=1_000))

    engine = TriangularArbitrageEngine(exchange="binance", store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    opp = next(o for o in opportunities if o.symbol == "USDT->BTC->ETH->USDT")
    expected_fee_per_leg = engine.fee_engine.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=False)
    expected_net_profit = (opp.gross_spread_pct / 100 * opp.capital_usd) - 3 * expected_fee_per_leg
    assert opp.expected_profit_usd == pytest.approx(expected_net_profit, rel=1e-6)
