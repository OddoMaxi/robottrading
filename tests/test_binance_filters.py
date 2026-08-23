from app.execution.binance_filters import SymbolNotFound, parse_symbol_rules, round_down_to_step, validate_order

EXCHANGE_INFO_FIXTURE = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 8,
            "quoteAssetPrecision": 8,
            "orderTypes": ["LIMIT", "MARKET", "LIMIT_MAKER"],
            "isSpotTradingAllowed": True,
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.0", "stepSize": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5.00", "applyMinToMarket": True},
            ],
        },
        {
            "symbol": "HALTEDCOIN",
            "status": "BREAK",
            "baseAsset": "HALTED",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 8,
            "quoteAssetPrecision": 8,
            "orderTypes": ["LIMIT"],
            "isSpotTradingAllowed": False,
            "filters": [],
        },
    ]
}


def test_parse_symbol_rules_reads_real_binance_filter_shape():
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")
    assert rules.status == "TRADING"
    assert rules.min_qty == 0.00001
    assert rules.step_size == 0.00001
    assert rules.tick_size == 0.01
    assert rules.min_notional == 5.00
    assert rules.is_spot_trading_allowed is True
    assert "MARKET" in rules.order_types


def test_parse_symbol_rules_raises_for_unknown_symbol():
    try:
        parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "NOPE"), "should have raised"
        assert False, "expected SymbolNotFound"
    except SymbolNotFound:
        pass


def test_round_down_to_step_never_rounds_up():
    assert round_down_to_step(0.123456, 0.00001) == 0.12345
    assert round_down_to_step(1.0, 0.1) == 1.0
    assert round_down_to_step(0.0, 0.00001) == 0.0


def test_validate_order_executable_when_all_constraints_pass():
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")
    result = validate_order(rules, "BUY", price=50_000.0, requested_qty=0.001, available_quote_balance_usdt=100.0)
    assert result.executable is True
    assert result.reason is None
    assert result.min_notional_pass is True
    assert result.lot_size_pass is True
    assert result.balance_pass is True


def test_validate_order_rejects_below_min_notional():
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")
    # 0.00001 BTC * 50,000 = 0.50 USDT, well under the 5.00 minNotional
    result = validate_order(rules, "BUY", price=50_000.0, requested_qty=0.00001, available_quote_balance_usdt=100.0)
    assert result.executable is False
    assert "MIN_NOTIONAL" in result.reason or "minNotional" in result.reason or "notional" in result.reason.lower()
    assert result.min_notional_pass is False


def test_validate_order_rejects_below_min_qty():
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")
    result = validate_order(rules, "BUY", price=50_000.0, requested_qty=0.000001, available_quote_balance_usdt=100.0)
    assert result.executable is False
    assert result.lot_size_pass is False


def test_validate_order_rejects_insufficient_balance():
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")
    result = validate_order(rules, "BUY", price=50_000.0, requested_qty=0.001, available_quote_balance_usdt=1.0)
    assert result.executable is False
    assert result.balance_pass is False


def test_validate_order_rejects_halted_symbol():
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "HALTEDCOIN")
    result = validate_order(rules, "BUY", price=1.0, requested_qty=10.0, available_quote_balance_usdt=100.0)
    assert result.executable is False
    assert "TRADING" in result.reason


def test_validate_order_sell_side_does_not_check_quote_balance():
    """A SELL doesn't cost quote-asset balance up front — balance_pass
    must not spuriously fail a SELL based on the quote balance."""
    rules = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")
    result = validate_order(rules, "SELL", price=50_000.0, requested_qty=0.001, available_quote_balance_usdt=0.0)
    assert result.balance_pass is True
