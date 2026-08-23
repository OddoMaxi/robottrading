from app.execution.bybit_client import _parse_api_key_info, _parse_book_ticker, _parse_fee_rate, _parse_symbol_rules

READ_ONLY_KEY_INFO_FIXTURE = {
    "result": {
        "readOnly": 1,
        "ips": ["147.93.56.10"],
        "permissions": {"ContractTrade": [], "Spot": [], "Wallet": [], "Options": [], "Derivatives": []},
    }
}

TRADE_ENABLED_KEY_INFO_FIXTURE = {
    "result": {
        "readOnly": 0,
        "ips": [],
        "permissions": {"ContractTrade": [], "Spot": ["SpotTrade"], "Wallet": [], "Options": [], "Derivatives": []},
    }
}

TICKER_FIXTURE = {"result": {"list": [{"symbol": "LUNCUSDT", "bid1Price": "0.00005440", "ask1Price": "0.00005461"}]}}

INSTRUMENTS_FIXTURE = {
    "result": {
        "list": [
            {
                "symbol": "LUNCUSDT",
                "status": "Trading",
                "lotSizeFilter": {"basePrecision": "1", "minOrderQty": "100", "maxOrderQty": "500000000000", "minOrderAmt": "1"},
                "priceFilter": {"tickSize": "0.00000001"},
            },
            {
                "symbol": "HALTEDCOIN",
                "status": "Closed",
                "lotSizeFilter": {"basePrecision": "1", "minOrderQty": "1", "maxOrderQty": "1000"},
                "priceFilter": {"tickSize": "0.01"},
            },
        ]
    }
}

FEE_FIXTURE = {"result": {"list": [{"symbol": "LUNCUSDT", "makerFeeRate": "0.001", "takerFeeRate": "0.001"}]}}


def test_parse_book_ticker_finds_symbol():
    ticker = _parse_book_ticker(TICKER_FIXTURE, "LUNCUSDT")
    assert ticker is not None
    assert ticker.bid_price == 0.00005440
    assert ticker.ask_price == 0.00005461


def test_parse_book_ticker_returns_none_for_missing_symbol():
    assert _parse_book_ticker(TICKER_FIXTURE, "ETHUSDT") is None


def test_parse_symbol_rules_trading_status():
    rules = _parse_symbol_rules(INSTRUMENTS_FIXTURE, "LUNCUSDT")
    assert rules is not None
    assert rules.is_tradable is True
    assert rules.min_order_qty == 100.0
    assert rules.min_order_amt == 1.0
    assert rules.tick_size == 0.00000001


def test_parse_symbol_rules_non_trading_status_is_flagged():
    rules = _parse_symbol_rules(INSTRUMENTS_FIXTURE, "HALTEDCOIN")
    assert rules is not None
    assert rules.is_tradable is False


def test_parse_fee_rate():
    fee = _parse_fee_rate(FEE_FIXTURE, "LUNCUSDT", now=0.0)
    assert fee is not None
    assert fee.maker_fee_rate == 0.001
    assert fee.taker_fee_rate == 0.001


def test_parse_fee_rate_missing_symbol_returns_none():
    assert _parse_fee_rate(FEE_FIXTURE, "ETHUSDT", now=0.0) is None


def test_parse_api_key_info_read_only_key():
    info = _parse_api_key_info(READ_ONLY_KEY_INFO_FIXTURE, now=0.0)
    assert info.read_only is True
    assert info.ip_restricted is True
    assert info.has_any_trade_or_withdraw_permission() is False


def test_parse_api_key_info_flags_trade_enabled_key():
    """This is the field item 2's safety requirement must actually be
    checked against — not any account-wide status field."""
    info = _parse_api_key_info(TRADE_ENABLED_KEY_INFO_FIXTURE, now=0.0)
    assert info.read_only is False
    assert info.has_any_trade_or_withdraw_permission() is True
