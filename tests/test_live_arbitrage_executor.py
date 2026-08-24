import uuid

import pytest

import app.execution.live_arbitrage_executor as executor_module
from app.execution.binance_live_trade_client import BinanceOrderFill, BinanceOrderResult
from app.execution.bybit_live_trade_client import BybitOrderAck, BybitOrderStatus
from app.execution.live_arbitrage_executor import ArbitrageOutcome, LiveArbitrageExecutor
from app.execution.live_guard import LiveTradingGuard

SYMBOL = "LUNCUSDT"

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

BYBIT_RULES = type(
    "Rules", (), {"is_tradable": True, "min_order_qty": 100.0, "qty_step": 1.0, "tick_size": 0.00000001, "min_order_amt": 1.0}
)()

DEEP_ASKS = [(0.00005461, 500_000_000.0), (0.00005470, 500_000_000.0)]
DEEP_BIDS = [(0.00005600, 500_000_000.0), (0.00005590, 500_000_000.0)]  # healthy spread over the ask


class FakeBinanceRead:
    async def get_book_ticker(self, symbol):
        return {"bidPrice": "0.00005440", "askPrice": "0.00005461"}

    async def get_order_book_depth(self, symbol, limit=20):
        return {"asks": [[str(p), str(q)] for p, q in DEEP_ASKS], "bids": []}

    async def get_exchange_info(self, symbols=None):
        return EXCHANGE_INFO_FIXTURE

    async def get_trade_fee(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()


class FakeBybitRead:
    async def get_book_ticker(self, symbol):
        return type("Ticker", (), {"bid_price": 0.00005600, "ask_price": 0.00005620})()

    async def get_order_book_depth(self, symbol, limit=50):
        return {"result": {"a": [], "b": [[str(p), str(q)] for p, q in DEEP_BIDS]}}

    async def get_symbol_rules(self, symbol):
        return BYBIT_RULES

    async def get_fee_rate(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()


class FakeBinanceTrade:
    def __init__(self, fill_status="FILLED", fill_qty=183000.0, never_terminal=False, raise_on_submit=False):
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.never_terminal = never_terminal
        self.raise_on_submit = raise_on_submit
        self.submitted_orders = []

    async def place_market_order(self, symbol, side, client_order_id, quantity=None, quote_order_qty=None):
        if self.raise_on_submit:
            raise RuntimeError("simulated network failure")
        self.submitted_orders.append((symbol, side, client_order_id, quantity, quote_order_qty))
        return BinanceOrderResult(
            symbol=symbol, order_id=1, client_order_id=client_order_id, status="NEW", executed_qty=0.0,
            cumulative_quote_qty=0.0, fills=[], raw={},
        )

    async def get_order_status(self, symbol, order_id=None, orig_client_order_id=None):
        if self.never_terminal:
            return BinanceOrderResult(
                symbol=symbol, order_id=1, client_order_id=orig_client_order_id or "", status="NEW",
                executed_qty=0.0, cumulative_quote_qty=0.0, fills=[], raw={},
            )
        if self.fill_qty <= 0:
            return BinanceOrderResult(
                symbol=symbol, order_id=1, client_order_id=orig_client_order_id or "", status="REJECTED",
                executed_qty=0.0, cumulative_quote_qty=0.0, fills=[], raw={},
            )
        return BinanceOrderResult(
            symbol=symbol, order_id=1, client_order_id=orig_client_order_id or "", status=self.fill_status,
            executed_qty=self.fill_qty, cumulative_quote_qty=self.fill_qty * 0.0000546,
            fills=[BinanceOrderFill(price=0.0000546, qty=self.fill_qty, commission=0.01, commission_asset="USDT")], raw={},
        )


class FakeBybitTrade:
    def __init__(self, fill_status="Filled", fill_qty=183000.0, never_terminal=False, raise_on_submit=False):
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.never_terminal = never_terminal
        self.raise_on_submit = raise_on_submit
        self.submitted_orders = []

    async def place_market_order(self, symbol, side, qty, order_link_id):
        if self.raise_on_submit:
            raise RuntimeError("simulated network failure")
        self.submitted_orders.append((symbol, side, qty, order_link_id))
        return BybitOrderAck(order_id="bybit-1", order_link_id=order_link_id, raw={})

    async def get_order_status(self, symbol, order_id=None, order_link_id=None):
        if self.never_terminal:
            return BybitOrderStatus(
                order_id="bybit-1", order_link_id=order_link_id or "", symbol=symbol, side="Sell",
                order_status="New", cum_exec_qty=0.0, cum_exec_value=0.0, cum_exec_fee=0.0, avg_price=None, raw={},
            )
        if self.fill_qty <= 0:
            return BybitOrderStatus(
                order_id="bybit-1", order_link_id=order_link_id or "", symbol=symbol, side="Sell",
                order_status="Rejected", cum_exec_qty=0.0, cum_exec_value=0.0, cum_exec_fee=0.0, avg_price=None, raw={},
            )
        return BybitOrderStatus(
            order_id="bybit-1", order_link_id=order_link_id or "", symbol=symbol, side="Sell",
            order_status=self.fill_status, cum_exec_qty=self.fill_qty, cum_exec_value=self.fill_qty * 0.0000560,
            cum_exec_fee=0.01, avg_price=0.0000560, raw={},
        )


def _executor(binance_trade=None, bybit_trade=None):
    return LiveArbitrageExecutor(
        binance_read=FakeBinanceRead(),
        binance_trade=binance_trade or FakeBinanceTrade(),
        bybit_read=FakeBybitRead(),
        bybit_trade=bybit_trade or FakeBybitTrade(),
    )


def _armed_guard(**overrides):
    base = dict(
        live_trading_enabled=True,
        max_live_capital_usdt=10.0,
        symbol_allowlist=[],  # PHASE 3: empty = unrestricted, matches the "no hardcoded list" default
        allowed_directions=[],
        max_notional_per_leg_usdt=10.0,
        max_concurrent_arbitrages=1,
    )
    base.update(overrides)
    return LiveTradingGuard(**base)


async def test_refuses_when_live_trading_disabled(monkeypatch):
    guard = _armed_guard(live_trading_enabled=False)
    monkeypatch.setattr(executor_module, "live_guard", guard)
    result = await _executor().execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.NO_TRADE_REFUSED
    assert result.buy_client_order_id is None


async def test_no_order_submitted_when_refused(monkeypatch):
    guard = _armed_guard(live_trading_enabled=False)
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade()
    await _executor(binance_trade=binance_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert binance_trade.submitted_orders == []


async def test_in_flight_count_returns_to_zero_after_refused_attempt(monkeypatch):
    guard = _armed_guard(live_trading_enabled=False)
    monkeypatch.setattr(executor_module, "live_guard", guard)
    await _executor().execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert guard.in_flight_count == 0


async def test_both_legs_filled_computes_actual_pnl(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    result = await _executor().execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.actual_net_pnl_usd is not None
    assert result.actual_net_pnl_usd > 0  # the fixture prices have a healthy Bybit-over-Binance spread
    assert guard.in_flight_count == 0


async def test_buy_leg_rejected_with_zero_fill_is_no_fill_not_neutralized(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=0.0)
    result = await _executor(binance_trade=binance_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.NO_FILL
    assert result.neutralization_order_id is None


async def test_sell_leg_submission_error_triggers_neutralization(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(raise_on_submit=True)
    binance_trade = FakeBinanceTrade()
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BUY_ONLY_NEUTRALIZED
    assert result.neutralization_order_id is not None
    # the buy leg AND the neutralization sell were both submitted to Binance — never retried on Bybit
    assert len(binance_trade.submitted_orders) == 2
    assert binance_trade.submitted_orders[1][1] == "SELL"


async def test_sell_leg_rejected_triggers_neutralization(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(fill_qty=0.0)
    result = await _executor(bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BUY_ONLY_NEUTRALIZED


async def test_buy_leg_timeout_engages_kill_switch_and_never_submits_sell(monkeypatch):
    monkeypatch.setattr(executor_module, "LEG_CONFIRMATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(executor_module, "LEG_POLL_INTERVAL_SECONDS", 0.01)
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(never_terminal=True)
    bybit_trade = FakeBybitTrade()
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.UNKNOWN_BUY_LEG
    assert guard.kill_switch_engaged is True
    assert bybit_trade.submitted_orders == []  # never attempts the sell leg on an unknown buy outcome


async def test_sell_leg_timeout_engages_kill_switch_without_blind_retry(monkeypatch):
    monkeypatch.setattr(executor_module, "LEG_CONFIRMATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(executor_module, "LEG_POLL_INTERVAL_SECONDS", 0.01)
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(never_terminal=True)
    result = await _executor(bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.UNKNOWN_SELL_LEG
    assert guard.kill_switch_engaged is True
    # exactly one sell submission — the whole point is it must NEVER retry blindly on ambiguity
    assert len(bybit_trade.submitted_orders) == 1


async def test_neutralization_failure_is_marked_critical(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(fill_qty=0.0)  # forces neutralization
    binance_trade = FakeBinanceTrade()
    # After the buy leg fills once, make every subsequent get_order_status call
    # (used by neutralization too) report the never-terminal NEW status.
    original_get_status = binance_trade.get_order_status
    call_count = {"n": 0}

    async def flaky_get_status(symbol, order_id=None, orig_client_order_id=None):
        call_count["n"] += 1
        if call_count["n"] <= 1:
            return await original_get_status(symbol, order_id, orig_client_order_id)
        from app.execution.binance_live_trade_client import BinanceOrderResult

        return BinanceOrderResult(
            symbol=symbol, order_id=2, client_order_id=orig_client_order_id or "", status="NEW",
            executed_qty=0.0, cumulative_quote_qty=0.0, fills=[], raw={},
        )

    monkeypatch.setattr(executor_module, "LEG_CONFIRMATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(executor_module, "LEG_POLL_INTERVAL_SECONDS", 0.01)
    binance_trade.get_order_status = flaky_get_status
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.NEUTRALIZATION_FAILED
    assert guard.kill_switch_engaged is True


async def test_reverse_direction_bybit_buy_binance_sell_also_works(monkeypatch):
    """PHASE 3 (user directive, 2026-08-23): 'scanner les deux
    directions' — Bybit buy -> Binance sell must be just as fully
    supported as the original Binance-buy/Bybit-sell direction."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    # swap the fixtures' relative pricing so buying on bybit (ask) and
    # selling on binance (bid) is the profitable direction
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)

    class CheapBybitRead(FakeBybitRead):
        async def get_book_ticker(self, symbol):
            return type("Ticker", (), {"bid_price": 0.00005430, "ask_price": 0.00005440})()

    class RichBinanceRead(FakeBinanceRead):
        async def get_book_ticker(self, symbol):
            return {"bidPrice": "0.00005600", "askPrice": "0.00005610"}

        async def get_order_book_depth(self, symbol, limit=20):
            return {"asks": [], "bids": [[str(p), str(q)] for p, q in DEEP_BIDS]}

    executor = LiveArbitrageExecutor(
        binance_read=RichBinanceRead(), binance_trade=binance_trade, bybit_read=CheapBybitRead(), bybit_trade=bybit_trade
    )
    result = await executor.execute_one_arbitrage(SYMBOL, "bybit", "binance", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.buy_exchange == "bybit"
    assert result.sell_exchange == "binance"
    assert bybit_trade.submitted_orders[0][1] == "Buy"
    assert binance_trade.submitted_orders[0][1] == "SELL"
    # the point of this test is the exchange/side dispatch, not the fixtures'
    # fill-price realism — just confirm the full flow completed and computed a P&L
    assert result.actual_net_pnl_usd is not None


async def test_bybit_buy_leg_transmits_the_raw_usdt_notional_not_a_converted_base_qty(monkeypatch):
    """Bybit BUY caller fix (2026-08-24): bybit_live_trade_client.place_
    market_order now sends marketUnit="quoteCoin" for every Buy, meaning
    qty on the wire IS the USDT notional. This caller must pass
    requested_notional_per_leg_usdt straight through when Bybit is the
    buy exchange — never pre-convert it to an estimated base-asset
    quantity via a book price first."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)

    class CheapBybitRead(FakeBybitRead):
        async def get_book_ticker(self, symbol):
            return type("Ticker", (), {"bid_price": 0.00005430, "ask_price": 0.00005440})()

    class RichBinanceRead(FakeBinanceRead):
        async def get_book_ticker(self, symbol):
            return {"bidPrice": "0.00005600", "askPrice": "0.00005610"}

        async def get_order_book_depth(self, symbol, limit=20):
            return {"asks": [], "bids": [[str(p), str(q)] for p, q in DEEP_BIDS]}

    executor = LiveArbitrageExecutor(
        binance_read=RichBinanceRead(), binance_trade=binance_trade, bybit_read=CheapBybitRead(), bybit_trade=bybit_trade
    )
    result = await executor.execute_one_arbitrage(SYMBOL, "bybit", "binance", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert bybit_trade.submitted_orders[0][1] == "Buy"
    assert bybit_trade.submitted_orders[0][2] == 10.0  # the raw USDT notional — NOT 10.0 / 0.00005440 (an estimated LUNC quantity)


async def test_bybit_sell_leg_still_transmits_a_base_asset_quantity(monkeypatch):
    """SELL was already correct before this fix and must stay that way —
    qty is the real base-asset fill quantity from the buy leg, never the
    USDT notional."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert bybit_trade.submitted_orders[0][1] == "Sell"
    sell_qty = bybit_trade.submitted_orders[0][2]
    assert sell_qty == pytest.approx(183000.0, rel=0.01)  # the real base-asset fill qty, not a USDT amount (~10)


async def test_neither_caller_double_converts_buy_notional_into_a_second_unit(monkeypatch):
    """Combined regression: across both directions, the qty Bybit
    receives for a Buy always equals exactly the requested USDT notional
    passed into execute_one_arbitrage — proving there is no leftover
    price-based conversion anywhere on the buy path in either direction."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)

    # binance buy / bybit sell: Bybit never receives a Buy order here.
    default_bybit_trade = FakeBybitTrade()
    await _executor(bybit_trade=default_bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert all(order[1] != "Buy" for order in default_bybit_trade.submitted_orders)

    # bybit buy / binance sell: Bybit's Buy order qty must be exactly 10.0, not a price-derived figure.
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)

    class CheapBybitRead(FakeBybitRead):
        async def get_book_ticker(self, symbol):
            return type("Ticker", (), {"bid_price": 0.00005430, "ask_price": 0.00005440})()

    class RichBinanceRead(FakeBinanceRead):
        async def get_book_ticker(self, symbol):
            return {"bidPrice": "0.00005600", "askPrice": "0.00005610"}

        async def get_order_book_depth(self, symbol, limit=20):
            return {"asks": [], "bids": [[str(p), str(q)] for p, q in DEEP_BIDS]}

    executor = LiveArbitrageExecutor(
        binance_read=RichBinanceRead(), binance_trade=binance_trade, bybit_read=CheapBybitRead(), bybit_trade=bybit_trade
    )
    await executor.execute_one_arbitrage(SYMBOL, "bybit", "binance", 10.0)
    buy_orders = [order for order in bybit_trade.submitted_orders if order[1] == "Buy"]
    assert len(buy_orders) == 1
    assert buy_orders[0][2] == 10.0


async def test_execute_one_arbitrage_rejects_same_exchange_both_legs():
    executor = LiveArbitrageExecutor()
    with pytest.raises(ValueError):
        await executor.execute_one_arbitrage(SYMBOL, "binance", "binance", 5.0)


async def test_no_fresh_dual_leg_data_means_no_trade_not_a_crash(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)

    class BrokenBybitRead(FakeBybitRead):
        async def get_book_ticker(self, symbol):
            return None

    result = await _executor().execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)  # sanity baseline still works
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED

    broken_executor = LiveArbitrageExecutor(
        binance_read=FakeBinanceRead(), binance_trade=FakeBinanceTrade(), bybit_read=BrokenBybitRead(), bybit_trade=FakeBybitTrade()
    )
    result = await broken_executor.execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.NO_TRADE_UNPROFITABLE
    assert guard.in_flight_count == 0
