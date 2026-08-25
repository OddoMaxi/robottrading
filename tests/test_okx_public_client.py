from app.scanner.okx_public_client import _parse_all_usdt_spot_symbols, _parse_book_ticker, _parse_symbol_rules, to_okx_symbol

TICKER_FIXTURE = {"data": [{"instId": "ZRO-USDT", "bidPx": "3.1200", "askPx": "3.1210"}]}
NO_QUOTE_FIXTURE = {"data": [{"instId": "ZRO-USDT", "bidPx": "", "askPx": ""}]}
INSTRUMENTS_FIXTURE = {"data": [{"instId": "ZRO-USDT", "state": "live", "minSz": "0.01", "lotSz": "0.01", "tickSz": "0.0001"}]}
HALTED_INSTRUMENTS_FIXTURE = {"data": [{"instId": "ZRO-USDT", "state": "suspend", "minSz": "0.01", "lotSz": "0.01", "tickSz": "0.0001"}]}
ALL_INSTRUMENTS_FIXTURE = {"data": [
    {"instId": "RVN-USDT", "baseCcy": "RVN", "quoteCcy": "USDT", "state": "live"},
    {"instId": "ZIL-USDT", "baseCcy": "ZIL", "quoteCcy": "USDT", "state": "live"},
    {"instId": "SUSPENDED-USDT", "baseCcy": "SUSPENDED", "quoteCcy": "USDT", "state": "suspend"},
    {"instId": "BTC-USDC", "baseCcy": "BTC", "quoteCcy": "USDC", "state": "live"},
]}


def test_to_okx_symbol_converts_slash_to_dash():
    assert to_okx_symbol("ZRO/USDT") == "ZRO-USDT"


def test_parse_book_ticker():
    ticker = _parse_book_ticker(TICKER_FIXTURE, "ZRO-USDT")
    assert ticker is not None
    assert ticker.bid_price == 3.12
    assert ticker.ask_price == 3.121


def test_parse_book_ticker_missing_symbol_returns_none():
    assert _parse_book_ticker(TICKER_FIXTURE, "STX-USDT") is None


def test_parse_book_ticker_empty_quote_returns_none():
    """An instId that exists but has no active quote (e.g. thin/delisted)
    must not fabricate a 0.0 price."""
    assert _parse_book_ticker(NO_QUOTE_FIXTURE, "ZRO-USDT") is None


def test_parse_symbol_rules_live_state_is_tradable():
    rules = _parse_symbol_rules(INSTRUMENTS_FIXTURE, "ZRO-USDT")
    assert rules is not None
    assert rules.is_tradable is True
    assert rules.min_qty == 0.01


def test_parse_symbol_rules_non_live_state_is_not_tradable():
    rules = _parse_symbol_rules(HALTED_INSTRUMENTS_FIXTURE, "ZRO-USDT")
    assert rules is not None
    assert rules.is_tradable is False


def test_parse_all_usdt_spot_symbols_only_live_usdt_pairs():
    symbols = _parse_all_usdt_spot_symbols(ALL_INSTRUMENTS_FIXTURE)
    assert symbols == {"RVN/USDT", "ZIL/USDT"}
