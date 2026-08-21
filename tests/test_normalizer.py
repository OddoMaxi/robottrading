from app.market_data.normalizer import (
    normalize_binance_book_ticker,
    normalize_binance_partial_depth,
    normalize_bybit_orderbook,
    normalize_okx_ticker,
)


def test_normalize_binance_book_ticker():
    quote = normalize_binance_book_ticker({"s": "BTCUSDT", "b": "99990.10", "B": "1.5", "a": "100000.20", "A": "2.1"})
    assert quote.exchange == "binance"
    assert quote.symbol == "BTC/USDT"
    assert quote.bid == 99990.10
    assert quote.ask == 100000.20


def test_normalize_okx_ticker():
    quote = normalize_okx_ticker(
        {"instId": "BTC-USDT", "bidPx": "99990.1", "bidSz": "1.2", "askPx": "100000.2", "askSz": "0.8", "ts": "1700000000000"}
    )
    assert quote.exchange == "okx"
    assert quote.symbol == "BTC/USDT"
    assert quote.exchange_timestamp == 1700000000.0


def test_normalize_okx_ticker_missing_book_returns_none():
    assert normalize_okx_ticker({"instId": "BTC-USDT", "bidPx": "", "askPx": "100000.2", "ts": "1700000000000"}) is None


def test_normalize_bybit_orderbook():
    quote = normalize_bybit_orderbook(
        {"s": "BTCUSDT", "b": [["99990.1", "1.2"]], "a": [["100000.2", "0.8"]]},
        ts_ms=1700000000000,
    )
    assert quote.exchange == "bybit"
    assert quote.symbol == "BTC/USDT"
    assert quote.bid == 99990.1
    assert quote.ask == 100000.2
    assert quote.exchange_timestamp == 1700000000.0


def test_normalize_bybit_orderbook_missing_side_returns_none():
    assert normalize_bybit_orderbook({"s": "BTCUSDT", "b": [], "a": [["100000.2", "0.8"]]}, ts_ms=1700000000000) is None


def test_normalize_binance_partial_depth():
    book = normalize_binance_partial_depth(
        "BTC/USDT",
        {
            "lastUpdateId": 160,
            "bids": [["99990.1", "1.2"], ["99989.0", "3.0"]],
            "asks": [["100000.2", "0.8"], ["100001.0", "2.5"]],
        },
    )
    assert book.exchange == "binance"
    assert book.symbol == "BTC/USDT"
    assert len(book.bids) == 2
    assert len(book.asks) == 2
    assert book.bids[0].price == 99990.1
    assert book.bids[0].quantity == 1.2
    assert book.asks[1].price == 100001.0


def test_normalize_binance_partial_depth_missing_side_returns_none():
    assert normalize_binance_partial_depth("BTC/USDT", {"lastUpdateId": 1, "bids": [], "asks": [["1", "1"]]}) is None
