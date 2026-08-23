from app.execution.bybit_client import (
    _parse_api_key_info,
    _parse_book_ticker,
    _parse_fee_rate,
    _parse_symbol_rules,
    parse_all_wallet_balances,
    parse_wallet_balance,
)

WALLET_BALANCE_FIXTURE = {
    "result": {
        "list": [
            {
                "accountType": "UNIFIED",
                "coin": [
                    {"coin": "LUNC", "walletBalance": "1000000", "availableToWithdraw": "950000"},
                    {"coin": "USDT", "walletBalance": "5.0", "availableToWithdraw": "5.0"},
                ],
            }
        ]
    }
}

READ_ONLY_KEY_INFO_FIXTURE = {
    "result": {
        "readOnly": 1,
        "ips": ["147.93.56.10"],
        "permissions": {"ContractTrade": [], "Spot": [], "Wallet": [], "Options": [], "Derivatives": []},
    }
}

# Real-world observed shape (Phase 2F, 2026-08-23): readOnly=1 with several
# category permissions still populated — category names share Bybit's
# internal "Trade" taxonomy but grant query-only access under a
# Read-Only key. Confirmed against the key-creation UI's own text
# ("Query order info for Spot trading only", etc.) — NOT a violation.
READ_ONLY_KEY_WITH_NAMED_CATEGORIES_FIXTURE = {
    "result": {
        "readOnly": 1,
        "ips": ["147.93.56.10"],
        "permissions": {
            "ContractTrade": ["Order", "Position"],
            "Spot": ["SpotTrade"],
            "Wallet": [],
            "Options": ["OptionsTrade"],
            "Derivatives": ["DerivativesTrade"],
        },
    }
}

TRADE_ENABLED_KEY_INFO_FIXTURE = {
    "result": {
        "readOnly": 0,
        "ips": [],
        "permissions": {"ContractTrade": [], "Spot": ["SpotTrade"], "Wallet": [], "Options": [], "Derivatives": []},
    }
}

WITHDRAWAL_ENABLED_KEY_INFO_FIXTURE = {
    "result": {
        "readOnly": 1,
        "ips": [],
        "permissions": {"ContractTrade": [], "Spot": [], "Wallet": ["Withdraw"], "Options": [], "Derivatives": []},
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
    assert info.is_safely_read_only() is True


def test_named_trade_categories_under_a_read_only_key_are_not_a_violation():
    """The real-world case that looked alarming at first: readOnly=1 but
    ContractTrade/Spot/Options/Derivatives all non-empty. Confirmed via
    Bybit's own key-creation UI text that these grant query-only access
    under a Read-Only key — must NOT be flagged."""
    info = _parse_api_key_info(READ_ONLY_KEY_WITH_NAMED_CATEGORIES_FIXTURE, now=0.0)
    assert info.read_only is True
    assert info.is_safely_read_only() is True


def test_parse_api_key_info_flags_trade_enabled_key():
    """This is the field item 2's safety requirement must actually be
    checked against — not any account-wide status field."""
    info = _parse_api_key_info(TRADE_ENABLED_KEY_INFO_FIXTURE, now=0.0)
    assert info.read_only is False
    assert info.is_safely_read_only() is False


def test_parse_wallet_balance_finds_asset():
    assert parse_wallet_balance(WALLET_BALANCE_FIXTURE, "LUNC") == 950000.0
    assert parse_wallet_balance(WALLET_BALANCE_FIXTURE, "USDT") == 5.0


def test_parse_wallet_balance_missing_asset_returns_zero():
    assert parse_wallet_balance(WALLET_BALANCE_FIXTURE, "BTC") == 0.0


def test_parse_all_wallet_balances_returns_every_nonzero_asset():
    assert parse_all_wallet_balances(WALLET_BALANCE_FIXTURE) == {"LUNC": 950000.0, "USDT": 5.0}


def test_parse_all_wallet_balances_excludes_zero_balances():
    data = {"result": {"list": [{"coin": [{"coin": "ZRO", "availableToWithdraw": "0"}, {"coin": "USDT", "availableToWithdraw": "5"}]}]}}
    assert parse_all_wallet_balances(data) == {"USDT": 5.0}


def test_parse_all_wallet_balances_empty_when_no_accounts():
    assert parse_all_wallet_balances({"result": {"list": []}}) == {}


def test_withdrawal_permission_is_always_a_real_violation():
    """Withdrawal has no read-only variant on Bybit — its presence must
    always be flagged, unlike the other category names."""
    info = _parse_api_key_info(WITHDRAWAL_ENABLED_KEY_INFO_FIXTURE, now=0.0)
    assert info.read_only is True  # even though the top-level flag says read-only
    assert info.has_withdrawal_permission() is True
    assert info.is_safely_read_only() is False
