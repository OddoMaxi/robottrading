import time

import pytest

from app.config.constants import MarketType
from app.engines.funding import FundingArbitrageEngine
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import FundingSnapshot, MarketDataStore


def make_spot_quote(bid: float, ask: float, qty: float = 10.0) -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange="binance", market=MarketType.SPOT, symbol="BTC/USDT",
        bid=bid, ask=ask, bid_quantity=qty, ask_quantity=qty,
        exchange_timestamp=now, received_at=now,
    )


def make_funding(funding_rate: float, mark_price: float) -> FundingSnapshot:
    now = time.time()
    return FundingSnapshot(
        exchange="binance", symbol="BTC/USDT", funding_rate=funding_rate,
        next_funding_time=now + 3600, mark_price=mark_price, index_price=mark_price, received_at=now,
    )


@pytest.mark.asyncio
async def test_positive_funding_and_basis_detected():
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))
    store.update_funding(make_funding(funding_rate=0.001, mark_price=100_050))  # positive funding + positive basis

    engine = FundingArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    opportunities = await engine.detect()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.gross_spread_pct > 0
    # net_spread_pct compounds funding income over the full holding period
    # (multiple funding events) net of one-time fees — it isn't simply
    # gross-minus-fees the way a single-shot spread is.
    assert opp.net_spread_pct > 0
    assert opp.legs[1]["market"] == "perpetual"


@pytest.mark.asyncio
async def test_negative_expected_return_not_traded():
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))
    store.update_funding(make_funding(funding_rate=-0.001, mark_price=99_950))  # negative funding + negative basis

    engine = FundingArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    assert await engine.detect() == []


@pytest.mark.asyncio
async def test_no_funding_data_yields_nothing():
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))

    engine = FundingArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    assert await engine.detect() == []


@pytest.mark.asyncio
async def test_stale_funding_snapshot_is_rejected_even_with_fresh_spot():
    """Market Data Quality Engine (Reality Engine spec, section 5) — the
    funding poller runs every ~30s; if it stalls for several cycles while
    spot keeps ticking, this must not keep pricing off an arbitrarily old
    funding rate."""
    store = MarketDataStore()
    store.update_quote(make_spot_quote(bid=99_990, ask=100_000))
    stale_funding = make_funding(funding_rate=0.001, mark_price=100_050)
    stale_funding.received_at = time.time() - 400  # >10 missed 30s polls
    store.update_funding(stale_funding)

    engine = FundingArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)
    assert await engine.detect() == []
