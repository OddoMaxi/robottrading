import uuid

from app.config.constants import Strategy
from app.execution.dual_leg_observer import DualLegObserver, _find_binance_and_mirror_legs
from app.opportunity.models import Opportunity

EXCHANGE_INFO_FIXTURE = {
    "symbols": [
        {
            "symbol": "LUNCUSDT",
            "status": "TRADING",
            "baseAsset": "LUNC",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 0,
            "quoteAssetPrecision": 8,
            "orderTypes": ["LIMIT", "MARKET"],
            "isSpotTradingAllowed": True,
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.00000001", "maxPrice": "1000.00", "tickSize": "0.00000001"},
                {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "9000000000.0", "stepSize": "1"},
                {"filterType": "NOTIONAL", "minNotional": "5.00", "applyMinToMarket": True},
            ],
        }
    ]
}

BYBIT_INSTRUMENTS_FIXTURE = {
    "result": {
        "list": [
            {
                "symbol": "LUNCUSDT",
                "status": "Trading",
                "lotSizeFilter": {"basePrecision": "1", "minOrderQty": "100", "maxOrderQty": "500000000000", "minOrderAmt": "1"},
                "priceFilter": {"tickSize": "0.00000001"},
            }
        ]
    }
}


def _opp(legs, symbol="LUNC/USDT", capital_usd=500.0):
    return Opportunity(strategy=Strategy.CROSS_EXCHANGE, symbol=symbol, legs=legs, gross_spread_pct=1.0, capital_usd=capital_usd, id=uuid.uuid4())


def test_find_binance_and_mirror_legs():
    legs = [{"exchange": "binance", "side": "buy"}, {"exchange": "bybit", "side": "sell"}]
    found = _find_binance_and_mirror_legs(_opp(legs))
    assert found is not None
    binance_leg, mirror_leg = found
    assert binance_leg["exchange"] == "binance"
    assert mirror_leg["exchange"] == "bybit"


def test_find_binance_and_mirror_legs_none_without_binance():
    legs = [{"exchange": "okx", "side": "buy"}, {"exchange": "bybit", "side": "sell"}]
    assert _find_binance_and_mirror_legs(_opp(legs)) is None


class FakeBinanceClient:
    async def get_book_ticker(self, symbol):
        return {"bidPrice": "0.00005440", "askPrice": "0.00005461"}

    async def get_exchange_info(self, symbols=None):
        return EXCHANGE_INFO_FIXTURE

    async def get_order_book_depth(self, symbol, limit=20):
        return {"asks": [["0.00005461", "500000000"]], "bids": [["0.00005440", "500000000"]]}

    async def get_trade_fee(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()


class FakeBybitClient:
    async def get_book_ticker(self, symbol):
        return type("Ticker", (), {"bid_price": 0.00005500, "ask_price": 0.00005520})()

    async def get_symbol_rules(self, symbol):
        return type(
            "Rules",
            (),
            {"is_tradable": True, "min_order_qty": 100.0, "qty_step": 1.0, "tick_size": 0.00000001, "min_order_amt": 1.0},
        )()

    async def get_order_book_depth(self, symbol, limit=50):
        return {"result": {"a": [["0.00005520", "500000000"]], "b": [["0.00005500", "500000000"]]}}

    async def get_fee_rate(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()


async def test_observe_returns_quote_for_binance_bybit_opportunity():
    observer = DualLegObserver(binance_client=FakeBinanceClient(), bybit_client=FakeBybitClient())
    legs = [{"exchange": "binance", "side": "buy"}, {"exchange": "bybit", "side": "sell"}]
    quote = await observer.observe(_opp(legs), micro_live_cap_usdt=10.0)
    assert quote is not None
    assert quote.buy_exchange == "binance"
    assert quote.sell_exchange == "bybit"
    assert quote.buy_fee_source == "real_account_fee"
    assert quote.sell_fee_source == "real_account_fee"


async def test_observe_skips_unsupported_mirror_exchange():
    observer = DualLegObserver(binance_client=FakeBinanceClient(), bybit_client=FakeBybitClient())
    legs = [{"exchange": "binance", "side": "buy"}, {"exchange": "okx", "side": "sell"}]
    quote = await observer.observe(_opp(legs), micro_live_cap_usdt=10.0)
    assert quote is None


async def test_observe_skips_when_neither_leg_is_binance():
    observer = DualLegObserver(binance_client=FakeBinanceClient(), bybit_client=FakeBybitClient())
    legs = [{"exchange": "okx", "side": "buy"}, {"exchange": "bybit", "side": "sell"}]
    quote = await observer.observe(_opp(legs), micro_live_cap_usdt=10.0)
    assert quote is None
