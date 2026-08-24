import app.execution.inventory_constitution_executor as executor_module
from app.execution.bybit_live_trade_client import BybitOrderAck, BybitOrderStatus
from app.execution.inventory_constitution_executor import (
    InventoryConstitutionExecutor,
    InventoryConstitutionOutcome,
    compute_required_inventory_qty,
)
from app.execution.inventory_guard import InventoryConstitutionGuard

SYMBOL = "RVNUSDT"

EXCHANGE_INFO_FIXTURE = {
    "symbols": [
        {
            "symbol": "RVNUSDT", "status": "TRADING", "baseAsset": "RVN", "quoteAsset": "USDT",
            "baseAssetPrecision": 0, "quoteAssetPrecision": 8, "orderTypes": ["MARKET"], "isSpotTradingAllowed": True,
            "filters": [
                {"filterType": "LOT_SIZE", "minQty": "1", "maxQty": "9e9", "stepSize": "0.1"},
                {"filterType": "NOTIONAL", "minNotional": "5.00"},
            ],
        }
    ]
}

BYBIT_RULES = type("Rules", (), {"is_tradable": True, "min_order_qty": 1.0, "qty_step": 0.1, "tick_size": 0.000001, "min_order_amt": 1.0})()

DEEP_ASKS = [(0.003400, 500_000.0), (0.003410, 500_000.0)]
DEEP_BIDS = [(0.003420, 500_000.0), (0.003415, 500_000.0)]  # healthy spread over the Binance ask


class FakeBinanceRead:
    async def get_book_ticker(self, symbol):
        return {"bidPrice": "0.003390", "askPrice": "0.003400"}

    async def get_order_book_depth(self, symbol, limit=20):
        return {"asks": [[str(p), str(q)] for p, q in DEEP_ASKS], "bids": []}

    async def get_exchange_info(self, symbols=None):
        return EXCHANGE_INFO_FIXTURE

    async def get_trade_fee(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()


class FakeBybitRead:
    async def get_book_ticker(self, symbol):
        return type("Ticker", (), {"bid_price": 0.003415, "ask_price": 0.003420})()

    async def get_order_book_depth(self, symbol, limit=50):
        return {"result": {"a": [], "b": [[str(p), str(q)] for p, q in DEEP_BIDS]}}

    async def get_symbol_rules(self, symbol):
        return BYBIT_RULES

    async def get_fee_rate(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()


class FakeBinanceTrade:
    async def place_market_order(self, *a, **k):
        raise AssertionError("this test never buys on binance — inventory constitution always buys on the SELL exchange")


class FakeBybitTrade:
    def __init__(self, fill_status="Filled", fill_qty=2900.0, never_terminal=False, raise_on_submit=False):
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
                order_id="bybit-1", order_link_id=order_link_id or "", symbol=symbol, side="Buy",
                order_status="New", cum_exec_qty=0.0, cum_exec_value=0.0, cum_exec_fee=0.0, avg_price=None, raw={},
            )
        if self.fill_qty <= 0:
            return BybitOrderStatus(
                order_id="bybit-1", order_link_id=order_link_id or "", symbol=symbol, side="Buy",
                order_status="Rejected", cum_exec_qty=0.0, cum_exec_value=0.0, cum_exec_fee=0.0, avg_price=None, raw={},
            )
        return BybitOrderStatus(
            order_id="bybit-1", order_link_id=order_link_id or "", symbol=symbol, side="Buy",
            order_status=self.fill_status, cum_exec_qty=self.fill_qty, cum_exec_value=self.fill_qty * 0.003420,
            cum_exec_fee=0.01, avg_price=0.003420, raw={},
        )


def _executor(bybit_trade=None):
    return InventoryConstitutionExecutor(
        binance_read=FakeBinanceRead(), binance_trade=FakeBinanceTrade(),
        bybit_read=FakeBybitRead(), bybit_trade=bybit_trade or FakeBybitTrade(),
    )


def _armed_guard(**overrides):
    base = dict(inventory_constitution_enabled=True, max_usdt_per_asset=10.0, max_concurrent_operations=1)
    base.update(overrides)
    return InventoryConstitutionGuard(**base)


# ---- compute_required_inventory_qty (pure) -------------------------------


def test_required_qty_covers_fee_and_margin_and_rounds_up_to_step():
    qty = compute_required_inventory_qty(bare_qty=2919.7, taker_fee_rate=0.001, step_size=0.1, technical_margin_pct=0.05)
    bare_with_fee = 2919.7 / 0.999
    expected_min = bare_with_fee * 1.05
    assert qty >= expected_min
    assert round(qty / 0.1, 6) == int(round(qty / 0.1, 6))  # exact multiple of the step


def test_required_qty_zero_bare_qty_is_zero():
    assert compute_required_inventory_qty(0.0, 0.001, 0.1) == 0.0


def test_required_qty_handles_zero_step_gracefully():
    qty = compute_required_inventory_qty(100.0, 0.001, 0.0)
    assert qty > 100.0  # still fee/margin-inflated, just not step-rounded


# ---- guard refusals (no order submitted) ----------------------------------


async def test_refuses_when_inventory_constitution_disabled(monkeypatch):
    guard = _armed_guard(inventory_constitution_enabled=False)
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    result = await _executor().constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.NO_TRADE_REFUSED
    assert "inventory_constitution_enabled is False" in result.reason


async def test_refuses_when_kill_switch_engaged(monkeypatch):
    guard = _armed_guard()
    guard.engage_kill_switch("test")
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    result = await _executor().constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.NO_TRADE_REFUSED


async def test_refuses_when_requested_exceeds_cap(monkeypatch):
    guard = _armed_guard(max_usdt_per_asset=10.0)
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    result = await _executor().constitute_inventory(SYMBOL, "binance", "bybit", 25.0)
    assert result.outcome == InventoryConstitutionOutcome.NO_TRADE_REFUSED
    assert "exceeds" in result.reason


async def test_refuses_when_already_at_max_concurrent(monkeypatch):
    guard = _armed_guard(max_concurrent_operations=1)
    guard.register_operation_start()
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    result = await _executor().constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.NO_TRADE_REFUSED
    assert "max_concurrent_inventory_operations" in result.reason


async def test_no_order_submitted_when_refused(monkeypatch):
    guard = _armed_guard(inventory_constitution_enabled=False)
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    trade = FakeBybitTrade()
    await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert trade.submitted_orders == []


async def test_rejects_same_exchange_raises_value_error():
    import pytest

    with pytest.raises(ValueError):
        await _executor().constitute_inventory(SYMBOL, "binance", "binance", 10.0)


class FlatBinanceRead(FakeBinanceRead):
    async def get_book_ticker(self, symbol):
        return {"bidPrice": "0.003400", "askPrice": "0.003400"}


class FlatBybitRead(FakeBybitRead):
    async def get_book_ticker(self, symbol):
        return type("Ticker", (), {"bid_price": 0.003400, "ask_price": 0.003400})()


async def test_no_trade_when_edge_insufficient_no_order_submitted(monkeypatch):
    """The fresh, immediate-pre-submission re-check (step 1 of the
    user's own numbered process) must block the order — a flat spread
    (buy price == sell price) can never clear real fees."""
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade()
    executor = InventoryConstitutionExecutor(
        binance_read=FlatBinanceRead(), bybit_read=FlatBybitRead(), bybit_trade=trade,
    )
    result = await executor.constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.NO_TRADE_EDGE_INSUFFICIENT
    assert trade.submitted_orders == []


# ---- happy path + fill verification ---------------------------------------


async def test_filled_records_real_price_qty_fee(monkeypatch):
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=2919.7)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.FILLED
    assert result.filled_qty == 2919.7
    assert result.avg_fill_price == 0.003420
    assert result.fee_usd == 0.01
    assert result.order_exchange_id == "bybit-1"
    assert len(trade.submitted_orders) == 1
    assert result.pre_purchase_net_edge_usd is not None
    assert result.pre_purchase_net_edge_usd > 0  # the fixture is set up as a genuinely positive spread


async def test_filled_and_edge_still_valid_marks_ready_for_arbitrage(monkeypatch):
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=3100.0)  # >= required qty
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.FILLED
    assert result.edge_still_valid_after_fill is True
    assert result.ready_for_arbitrage is True


async def test_never_places_a_second_order_regardless_of_ready_for_arbitrage(monkeypatch):
    """The module must never itself proceed to a second (arbitrage)
    order just because ready_for_arbitrage came back True."""
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=3100.0)
    await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert len(trade.submitted_orders) == 1  # exactly one order, ever


# ---- failure / timeout paths ----------------------------------------------


async def test_zero_fill_is_no_fill(monkeypatch):
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Rejected", fill_qty=0.0)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.NO_FILL


async def test_submission_error_engages_kill_switch(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    trade = FakeBybitTrade(raise_on_submit=True)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.UNKNOWN
    assert guard.kill_switch_engaged is True


async def test_timeout_engages_kill_switch_without_blind_retry(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    monkeypatch.setattr(executor_module, "LEG_CONFIRMATION_TIMEOUT_SECONDS", 0.05)
    monkeypatch.setattr(executor_module, "LEG_POLL_INTERVAL_SECONDS", 0.01)
    trade = FakeBybitTrade(never_terminal=True)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.UNKNOWN
    assert guard.kill_switch_engaged is True
    assert len(trade.submitted_orders) == 1  # exactly one submission, no retry after the timeout


# ---- in-flight bookkeeping --------------------------------------------------


async def test_in_flight_count_returns_to_zero_after_success(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    await _executor().constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert guard.in_flight_count == 0


async def test_in_flight_count_returns_to_zero_after_failure(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    trade = FakeBybitTrade(raise_on_submit=True)
    await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert guard.in_flight_count == 0


async def test_in_flight_count_not_incremented_when_refused(monkeypatch):
    guard = _armed_guard(inventory_constitution_enabled=False)
    monkeypatch.setattr(executor_module, "inventory_guard", guard)
    await _executor().constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert guard.in_flight_count == 0
