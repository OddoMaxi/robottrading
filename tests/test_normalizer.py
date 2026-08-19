from app.market_data.normalizer import normalize_binance_book_ticker, normalize_bybit_ticker, normalize_okx_ticker


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


def test_normalize_bybit_ticker():
    quote = normalize_bybit_ticker(
        {"symbol": "BTCUSDT", "bid1Price": "99990.1", "bid1Size": "1.2", "ask1Price": "100000.2", "ask1Size": "0.8"},
        ts_ms=1700000000000,
    )
    assert quote.exchange == "bybit"
    assert quote.symbol == "BTC/USDT"
    assert quote.exchange_timestamp == 1700000000.0


def test_normalize_bybit_ticker_delta_without_book_returns_none():
    assert normalize_bybit_ticker({"symbol": "BTCUSDT"}, ts_ms=1700000000000) is None
