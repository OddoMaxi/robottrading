from app.scanner.fast_discovery import (
    BulkTicker,
    discover_candidates,
    parse_binance_bulk_tickers,
    parse_bybit_bulk_tickers,
)

# ---- parsers -----------------------------------------------------------


def test_parse_binance_bulk_tickers_normal_entry():
    raw = [{"symbol": "ZROUSDT", "bidPrice": "3.09", "askPrice": "3.10", "quoteVolume": "1000000", "priceChangePercent": "5.2"}]
    tickers = parse_binance_bulk_tickers(raw)
    assert tickers["ZROUSDT"] == BulkTicker(symbol="ZROUSDT", bid=3.09, ask=3.10, quote_volume_24h=1000000.0, price_change_pct_24h=5.2)


def test_parse_binance_bulk_tickers_skips_zero_or_missing_prices():
    raw = [
        {"symbol": "A", "bidPrice": "0", "askPrice": "1", "quoteVolume": "1", "priceChangePercent": "0"},
        {"symbol": "B", "quoteVolume": "1"},  # missing bid/ask entirely
    ]
    assert parse_binance_bulk_tickers(raw) == {}


def test_parse_bybit_bulk_tickers_converts_fraction_to_percent():
    raw = {"result": {"list": [{"symbol": "ZROUSDT", "bid1Price": "3.16", "ask1Price": "3.17", "turnover24h": "500000", "price24hPcnt": "0.052"}]}}
    tickers = parse_bybit_bulk_tickers(raw)
    assert tickers["ZROUSDT"].price_change_pct_24h == 5.2
    assert tickers["ZROUSDT"].quote_volume_24h == 500000.0


def test_parse_bybit_bulk_tickers_skips_missing_prices():
    raw = {"result": {"list": [{"symbol": "X", "turnover24h": "1"}]}}
    assert parse_bybit_bulk_tickers(raw) == {}


# ---- discover_candidates ------------------------------------------------


def _ticker(bid, ask, volume=1000.0, change=0.0) -> BulkTicker:
    return BulkTicker(symbol="X", bid=bid, ask=ask, quote_volume_24h=volume, price_change_pct_24h=change)


def test_computes_both_directions_for_a_symbol_present_on_both_exchanges():
    binance = {"ZROUSDT": _ticker(bid=3.09, ask=3.10)}
    bybit = {"ZROUSDT": _ticker(bid=3.16, ask=3.17)}
    result = discover_candidates(["ZRO/USDT"], binance, bybit, min_raw_spread_pct=0.1, max_candidates=10)
    directions = {(c.buy_exchange, c.sell_exchange) for c in result.candidates}
    assert ("binance", "bybit") in directions or ("bybit", "binance") in directions


def test_symbol_missing_from_one_exchange_is_excluded():
    binance = {"ZROUSDT": _ticker(bid=3.09, ask=3.10)}
    bybit: dict = {}
    result = discover_candidates(["ZRO/USDT"], binance, bybit, min_raw_spread_pct=0.1, max_candidates=10)
    assert result.candidates == []
    assert result.raw_edge_count == 0
    assert result.fast_scanned_count == 1  # universe was still evaluated, just found nothing on Bybit


def test_binance_buy_direction_spread_computed_correctly():
    binance = {"ZROUSDT": _ticker(bid=3.00, ask=3.10)}  # buy on Binance at ask=3.10
    bybit = {"ZROUSDT": _ticker(bid=3.20, ask=3.30)}  # sell on Bybit at bid=3.20
    result = discover_candidates(["ZRO/USDT"], binance, bybit, min_raw_spread_pct=0.1, max_candidates=10)
    binance_buy = next(c for c in result.candidates if c.buy_exchange == "binance")
    expected_spread = (3.20 - 3.10) / 3.10 * 100
    assert binance_buy.raw_gross_spread_pct == expected_spread
    assert binance_buy.promoted_reason == "raw_spread"


def test_below_floor_direction_not_promoted():
    binance = {"ZROUSDT": _ticker(bid=3.10, ask=3.10)}
    bybit = {"ZROUSDT": _ticker(bid=3.10, ask=3.10)}  # zero spread either direction
    result = discover_candidates(["ZRO/USDT"], binance, bybit, min_raw_spread_pct=0.1, max_candidates=10, momentum_top_up=0)
    assert result.candidates == []
    assert result.raw_edge_count == 0


def test_candidates_sorted_by_spread_descending():
    binance = {
        "AUSDT": _ticker(bid=1.0, ask=1.0),
        "BUSDT": _ticker(bid=1.0, ask=1.0),
    }
    bybit = {
        "AUSDT": _ticker(bid=1.01, ask=1.01),  # +1% spread
        "BUSDT": _ticker(bid=1.05, ask=1.05),  # +5% spread
    }
    result = discover_candidates(["A/USDT", "B/USDT"], binance, bybit, min_raw_spread_pct=0.1, max_candidates=10)
    binance_buys = [c for c in result.candidates if c.buy_exchange == "binance"]
    assert binance_buys[0].symbol == "B/USDT"
    assert binance_buys[1].symbol == "A/USDT"


def test_max_candidates_caps_the_list():
    binance = {f"S{i}USDT": _ticker(bid=1.0, ask=1.0) for i in range(10)}
    bybit = {f"S{i}USDT": _ticker(bid=1.05, ask=1.05) for i in range(10)}
    universe = [f"S{i}/USDT" for i in range(10)]
    result = discover_candidates(universe, binance, bybit, min_raw_spread_pct=0.1, max_candidates=3)
    assert len(result.candidates) == 3
    # Only the binance-buy direction clears the floor here (bybit-buy is
    # negative, symmetric spread) — one qualifying direction per symbol.
    assert result.raw_edge_count == 10


def test_momentum_top_up_only_fills_remaining_slots_never_displaces_raw_spread():
    # One clean raw-spread winner, one flat-spread-but-high-momentum symbol.
    binance = {
        "WINUSDT": _ticker(bid=1.0, ask=1.0),
        "MOMOUSDT": _ticker(bid=1.0, ask=1.0, change=40.0),
    }
    bybit = {
        "WINUSDT": _ticker(bid=1.05, ask=1.05),
        "MOMOUSDT": _ticker(bid=1.0, ask=1.0, change=45.0),  # zero spread, huge momentum
    }
    result = discover_candidates(
        ["WIN/USDT", "MOMO/USDT"], binance, bybit,
        min_raw_spread_pct=0.1, max_candidates=10, momentum_top_up=5,
    )
    # A symbol can appear twice (once per direction) — key by (symbol,
    # buy_exchange) rather than symbol alone so directions aren't clobbered.
    reasons = {(c.symbol, c.buy_exchange): c.promoted_reason for c in result.candidates}
    assert reasons[("WIN/USDT", "binance")] == "raw_spread"
    assert reasons.get(("MOMO/USDT", "binance")) == "momentum"
    assert reasons.get(("MOMO/USDT", "bybit")) == "momentum"


def test_momentum_top_up_zero_means_no_extra_candidates():
    binance = {"MOMOUSDT": _ticker(bid=1.0, ask=1.0, change=90.0)}
    bybit = {"MOMOUSDT": _ticker(bid=1.0, ask=1.0, change=90.0)}
    result = discover_candidates(["MOMO/USDT"], binance, bybit, min_raw_spread_pct=0.1, max_candidates=10, momentum_top_up=0)
    assert result.candidates == []


def test_momentum_top_up_respects_max_candidates_cap():
    binance = {f"M{i}USDT": _ticker(bid=1.0, ask=1.0, change=float(i)) for i in range(5)}
    bybit = {f"M{i}USDT": _ticker(bid=1.0, ask=1.0, change=float(i)) for i in range(5)}
    universe = [f"M{i}/USDT" for i in range(5)]
    result = discover_candidates(universe, binance, bybit, min_raw_spread_pct=0.1, max_candidates=2, momentum_top_up=10)
    assert len(result.candidates) == 2
