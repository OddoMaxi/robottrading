from app.scanner.cross_exchange_scanner import scan_symbol
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher, SymbolMarketData


class FakeFetcher:
    def __init__(self, available: dict[str, SymbolMarketData]) -> None:
        self._available = available

    async def fetch(self, exchange, symbol):
        return self._available.get(exchange)


def _data(exchange, bid, ask, tradable=True):
    return SymbolMarketData(
        exchange=exchange,
        symbol="ZRO/USDT",
        best_bid=bid,
        best_ask=ask,
        ask_depth=[(ask, 100_000.0), (ask * 1.001, 100_000.0)],
        bid_depth=[(bid, 100_000.0), (bid * 0.999, 100_000.0)],
        min_qty=0.1,
        step_size=0.01,
        tick_size=0.0001,
        min_notional=5.0,
        tradable=tradable,
        maker_fee_rate=0.001,
        taker_fee_rate=0.001,
        fee_source="real_account_fee",
        fetched_at=100.0,
    )


async def test_scan_symbol_covers_all_ordered_pairs_of_reachable_exchanges():
    fetcher = FakeFetcher({"binance": _data("binance", 3.10, 3.11), "bybit": _data("bybit", 3.15, 3.16), "okx": _data("okx", 3.12, 3.13)})
    results = await scan_symbol(fetcher, "ZRO/USDT")
    directions = {(r.buy_exchange, r.sell_exchange) for r in results}
    # 3 exchanges -> 6 ordered pairs
    assert directions == {
        ("binance", "bybit"), ("bybit", "binance"),
        ("binance", "okx"), ("okx", "binance"),
        ("bybit", "okx"), ("okx", "bybit"),
    }
    assert len(results) == 6


async def test_scan_symbol_skips_unreachable_exchange_gracefully():
    fetcher = FakeFetcher({"binance": _data("binance", 3.10, 3.11), "bybit": _data("bybit", 3.15, 3.16)})  # okx unavailable
    results = await scan_symbol(fetcher, "ZRO/USDT")
    directions = {(r.buy_exchange, r.sell_exchange) for r in results}
    assert directions == {("binance", "bybit"), ("bybit", "binance")}
    assert all("okx" not in (r.buy_exchange, r.sell_exchange) for r in results)


async def test_scan_symbol_finds_the_profitable_direction():
    """Buying on binance (ask 3.10) and selling on bybit (bid 3.16) has a
    healthy spread; the reverse direction should not."""
    fetcher = FakeFetcher({"binance": _data("binance", 3.09, 3.10), "bybit": _data("bybit", 3.16, 3.17)})
    results = await scan_symbol(fetcher, "ZRO/USDT")
    binance_to_bybit = next(r for r in results if r.buy_exchange == "binance" and r.sell_exchange == "bybit")
    bybit_to_binance = next(r for r in results if r.buy_exchange == "bybit" and r.sell_exchange == "binance")
    assert binance_to_bybit.quote.net_profit_usd > 0
    assert bybit_to_binance.quote.net_profit_usd < 0
