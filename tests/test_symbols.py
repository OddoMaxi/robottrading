from app.market_data.symbols import to_common_symbol, to_native_perp_symbol, to_native_symbol


def test_to_common_symbol_binance():
    assert to_common_symbol("binance", "BTCUSDT") == "BTC/USDT"
    assert to_common_symbol("bybit", "ETHUSDC") == "ETH/USDC"
    assert to_common_symbol("binance", "ETHBTC") == "ETH/BTC"


def test_to_common_symbol_okx():
    assert to_common_symbol("okx", "BTC-USDT") == "BTC/USDT"


def test_to_native_symbol_roundtrip():
    assert to_native_symbol("binance", "BTC/USDT") == "BTCUSDT"
    assert to_native_symbol("okx", "BTC/USDT") == "BTC-USDT"
    assert to_common_symbol("binance", to_native_symbol("binance", "SOL/USDT")) == "SOL/USDT"


def test_to_native_perp_symbol():
    assert to_native_perp_symbol("binance", "BTC/USDT") == "BTCUSDT"
    assert to_native_perp_symbol("okx", "BTC/USDT") == "BTC-USDT-SWAP"
    assert to_native_perp_symbol("bybit", "BTC/USDT") == "BTCUSDT"
