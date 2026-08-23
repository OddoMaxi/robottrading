import time

from app.execution.live_universe import LiveUniverseBuilder, _binance_usdt_symbols, _bybit_usdt_symbols

BINANCE_INFO_FIXTURE = {
    "symbols": [
        {"baseAsset": "BTC", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
        {"baseAsset": "ZRO", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": True},
        {"baseAsset": "HALTED", "quoteAsset": "USDT", "status": "BREAK", "isSpotTradingAllowed": True},
        {"baseAsset": "MARGINONLY", "quoteAsset": "USDT", "status": "TRADING", "isSpotTradingAllowed": False},
        {"baseAsset": "BTC", "quoteAsset": "EUR", "status": "TRADING", "isSpotTradingAllowed": True},
    ]
}

BYBIT_INFO_FIXTURE = {
    "result": {
        "list": [
            {"baseCoin": "BTC", "quoteCoin": "USDT", "status": "Trading"},
            {"baseCoin": "STX", "quoteCoin": "USDT", "status": "Trading"},
            {"baseCoin": "SUSPENDED", "quoteCoin": "USDT", "status": "Closed"},
        ]
    }
}


def test_binance_usdt_symbols_filters_correctly():
    symbols = _binance_usdt_symbols(BINANCE_INFO_FIXTURE)
    assert symbols == {"BTC/USDT", "ZRO/USDT"}


def test_bybit_usdt_symbols_filters_correctly():
    symbols = _bybit_usdt_symbols(BYBIT_INFO_FIXTURE)
    assert symbols == {"BTC/USDT", "STX/USDT"}


class FakeBinance:
    async def get_exchange_info(self, symbols=None):
        return BINANCE_INFO_FIXTURE


class FakeBybit:
    def __init__(self):
        self.pages_served = 0

    async def _pages(self):
        return [BYBIT_INFO_FIXTURE]


async def test_get_universe_computes_intersection(monkeypatch):
    builder = LiveUniverseBuilder(binance_client=FakeBinance(), bybit_client=FakeBybit())

    async def fake_bybit_all(self):
        return BYBIT_INFO_FIXTURE

    monkeypatch.setattr(LiveUniverseBuilder, "_bybit_all_usdt_instruments", fake_bybit_all)
    universe = await builder.get_universe()
    assert universe.common_symbols == ["BTC/USDT"]  # only BTC/USDT is on both
    assert universe.binance_symbol_count == 2
    assert universe.bybit_symbol_count == 2


async def test_get_universe_caches_within_refresh_interval(monkeypatch):
    call_count = {"n": 0}

    async def fake_bybit_all(self):
        call_count["n"] += 1
        return BYBIT_INFO_FIXTURE

    monkeypatch.setattr(LiveUniverseBuilder, "_bybit_all_usdt_instruments", fake_bybit_all)
    builder = LiveUniverseBuilder(binance_client=FakeBinance(), bybit_client=FakeBybit(), refresh_interval_seconds=300.0)
    await builder.get_universe()
    await builder.get_universe()
    assert call_count["n"] == 1  # second call served from cache


async def test_get_universe_never_silently_collapses_to_empty(monkeypatch):
    """A transient partial failure returning an empty intersection must
    keep serving the last known-good universe, not fabricate an empty
    (and therefore fully-blocking) one."""

    async def good_bybit(self):
        return BYBIT_INFO_FIXTURE

    monkeypatch.setattr(LiveUniverseBuilder, "_bybit_all_usdt_instruments", good_bybit)
    builder = LiveUniverseBuilder(binance_client=FakeBinance(), bybit_client=FakeBybit(), refresh_interval_seconds=0.0)
    first = await builder.get_universe()
    assert first.common_symbols == ["BTC/USDT"]

    class BrokenBinance:
        async def get_exchange_info(self, symbols=None):
            return {"symbols": []}  # simulates a degraded/empty response

    monkeypatch.setattr(LiveUniverseBuilder, "_bybit_all_usdt_instruments", good_bybit)
    builder._binance = BrokenBinance()
    second = await builder.get_universe(force_refresh=True)
    assert second.common_symbols == ["BTC/USDT"]  # kept the stale-but-good universe
