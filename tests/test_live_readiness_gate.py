from types import SimpleNamespace

from app.execution.live_readiness_gate import _smallest_common_order_size, build_first_live_gate_report

BINANCE_RULES = SimpleNamespace(min_notional=5.0, min_qty=1.0, step_size=1.0)
BYBIT_RULES = SimpleNamespace(min_order_amt=1.0, min_order_qty=100.0, qty_step=1.0)
REFERENCE_PRICE = 0.0000546


def test_smallest_common_order_size_uses_the_binding_binance_min_notional():
    size = _smallest_common_order_size(BINANCE_RULES, BYBIT_RULES, REFERENCE_PRICE, max_notional_usdt=10.0)
    assert size.reachable is True
    assert size.notional_usdt >= 5.0  # Binance's 5 USDT MIN_NOTIONAL dominates over Bybit's 1 USDT min


def test_smallest_common_order_size_unreachable_when_min_exceeds_cap():
    huge_binance_rules = SimpleNamespace(min_notional=50.0, min_qty=1.0, step_size=1.0)
    size = _smallest_common_order_size(huge_binance_rules, BYBIT_RULES, REFERENCE_PRICE, max_notional_usdt=10.0)
    assert size.reachable is False
    assert "exceeds the 10.0 USDT cap" in size.reason


def test_smallest_common_order_size_zero_price_is_unreachable():
    size = _smallest_common_order_size(BINANCE_RULES, BYBIT_RULES, 0.0, max_notional_usdt=10.0)
    assert size.reachable is False


class ReadOnlyBinance:
    async def get_api_restrictions(self):
        return SimpleNamespace(enable_spot_and_margin_trading=False, enable_withdrawals=False)

    async def get_account_snapshot(self):
        return SimpleNamespace(balance_usdt=lambda: 13.2)

    async def get_exchange_info(self, symbols=None):
        return {
            "symbols": [
                {
                    "symbol": "LUNCUSDT", "status": "TRADING", "baseAsset": "LUNC", "quoteAsset": "USDT",
                    "baseAssetPrecision": 0, "quoteAssetPrecision": 8, "orderTypes": ["MARKET"], "isSpotTradingAllowed": True,
                    "filters": [
                        {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "9e9", "stepSize": "1"},
                        {"filterType": "NOTIONAL", "minNotional": "5.00"},
                    ],
                }
            ]
        }

    async def get_book_ticker(self, symbol):
        return {"bidPrice": "0.00005440", "askPrice": "0.00005461"}


class ReadOnlyBybitReadOnlyKey:
    async def get_api_key_info(self):
        return SimpleNamespace(read_only=True, permissions={"Spot": ["SpotTrade"]}, has_withdrawal_permission=lambda: False)

    async def get_wallet_balance(self):
        return {"result": {"list": [{"coin": [{"coin": "LUNC", "availableToWithdraw": "950000"}]}]}}

    async def get_symbol_rules(self, symbol):
        return SimpleNamespace(min_order_amt=1.0, min_order_qty=100.0, qty_step=1.0, is_tradable=True)


class ReadOnlyBybitTradeCapableKey(ReadOnlyBybitReadOnlyKey):
    async def get_api_key_info(self):
        return SimpleNamespace(read_only=False, permissions={"Spot": ["SpotTrade"]}, has_withdrawal_permission=lambda: False)


async def test_gate_reports_not_ready_when_binance_key_is_read_only():
    report = await build_first_live_gate_report(binance_client=ReadOnlyBinance(), bybit_client=ReadOnlyBybitTradeCapableKey())
    assert report.binance_trade_api_ready is False
    assert report.ready_for_first_real_arbitrage is False


async def test_gate_reports_not_ready_when_bybit_key_is_read_only():
    class TradeCapableBinance(ReadOnlyBinance):
        async def get_api_restrictions(self):
            return SimpleNamespace(enable_spot_and_margin_trading=True, enable_withdrawals=False)

    report = await build_first_live_gate_report(binance_client=TradeCapableBinance(), bybit_client=ReadOnlyBybitReadOnlyKey())
    assert report.bybit_trade_api_ready is False
    assert report.ready_for_first_real_arbitrage is False


async def test_gate_never_declares_ready_when_withdrawal_permission_present():
    class TradeCapableBinance(ReadOnlyBinance):
        async def get_api_restrictions(self):
            return SimpleNamespace(enable_spot_and_margin_trading=True, enable_withdrawals=True)  # withdrawal enabled!

    class TradeCapableBybit(ReadOnlyBybitReadOnlyKey):
        async def get_api_key_info(self):
            return SimpleNamespace(read_only=False, permissions={"Spot": ["SpotTrade"]}, has_withdrawal_permission=lambda: False)

    report = await build_first_live_gate_report(binance_client=TradeCapableBinance(), bybit_client=TradeCapableBybit())
    assert report.withdrawals_disabled is False
    assert report.ready_for_first_real_arbitrage is False


async def test_gate_populates_real_balances():
    report = await build_first_live_gate_report(binance_client=ReadOnlyBinance(), bybit_client=ReadOnlyBybitReadOnlyKey())
    assert report.binance_usdt_balance == 13.2
    assert report.bybit_lunc_balance == 950000.0
