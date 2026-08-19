import time

from app.config.constants import MarketType
from app.market_data.normalizer import NormalizedQuote
from app.opportunity.false_opportunity_filter import check_leg_pair_sync, check_quote_freshness


def make_quote(received_at: float) -> NormalizedQuote:
    return NormalizedQuote(
        exchange="binance", market=MarketType.SPOT, symbol="BTC/USDT",
        bid=100.0, ask=100.1, bid_quantity=1.0, ask_quantity=1.0,
        exchange_timestamp=received_at, received_at=received_at,
    )


def test_fresh_quote_passes():
    now = time.time()
    check = check_quote_freshness(make_quote(now - 0.1), now)
    assert check.is_valid is True


def test_stale_quote_rejected():
    now = time.time()
    check = check_quote_freshness(make_quote(now - 5.0), now)
    assert check.is_valid is False
    assert "stale" in check.reason


def test_synced_legs_pass():
    now = time.time()
    check = check_leg_pair_sync(make_quote(now - 0.1), make_quote(now - 0.2), now)
    assert check.is_valid is True


def test_desynced_legs_rejected():
    now = time.time()
    # Both individually "fresh" (under the 2s staleness threshold), but
    # 1.5s apart from each other — not a real simultaneous spread.
    check = check_leg_pair_sync(make_quote(now - 0.1), make_quote(now - 1.6), now)
    assert check.is_valid is False
    assert "desynced" in check.reason


def test_one_stale_leg_rejects_the_pair():
    now = time.time()
    check = check_leg_pair_sync(make_quote(now - 0.1), make_quote(now - 5.0), now)
    assert check.is_valid is False
    assert "stale" in check.reason
