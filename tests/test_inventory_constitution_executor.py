import pytest

import app.execution.inventory_constitution_executor as executor_module
from app.execution.bybit_live_trade_client import BybitOrderAck, BybitOrderStatus
from app.execution.inventory_constitution_executor import (
    InventoryConstitutionExecutor,
    InventoryConstitutionOutcome,
    compute_max_safe_notional,
    compute_required_inventory_qty,
    net_base_qty_after_fee,
    resolve_fee,
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
    def __init__(self, fill_status="Filled", fill_qty=2900.0, never_terminal=False, raise_on_submit=False, fee_asset="USDT", fee_amount=0.01):
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.never_terminal = never_terminal
        self.raise_on_submit = raise_on_submit
        self.fee_asset = fee_asset
        self.fee_amount = fee_amount
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
            cum_exec_fee=self.fee_amount, avg_price=0.003420, raw={},
            cum_fee_detail={self.fee_asset: self.fee_amount} if self.fee_asset else {},
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


# ---- resolve_fee / net_base_qty_after_fee (pure) — FIX 1, 2026-08-24 ------
#
# Regression coverage for the first real Bybit fill, which reported
# 2917.9 RVN filled and a bare cum_exec_fee of 2.9179 — mislabeled as
# $2.9179 (a ~29% "fee") when it was actually 2.9179 RVN (~$0.01, a
# normal ~0.1% fee). The real wallet only ever held 2914.9821 RVN.


def test_resolve_fee_charged_in_base_asset():
    fee_asset, fee_amount, fee_usd = resolve_fee({"RVN": 2.9179}, base_asset="RVN", avg_fill_price=0.003427, log_prefix="test")
    assert fee_asset == "RVN"
    assert fee_amount == 2.9179
    assert fee_usd == pytest.approx(2.9179 * 0.003427)
    assert fee_usd < 0.02  # NOT ~$2.92 — the exact mislabeling this fixes


def test_resolve_fee_charged_in_quote_asset():
    fee_asset, fee_amount, fee_usd = resolve_fee({"USDT": 0.01}, base_asset="RVN", avg_fill_price=0.003427, log_prefix="test")
    assert fee_asset == "USDT"
    assert fee_amount == 0.01
    assert fee_usd == 0.01  # already USD, no conversion needed


def test_resolve_fee_no_fee_at_all():
    assert resolve_fee({}, base_asset="RVN", avg_fill_price=0.003427, log_prefix="test") == (None, 0.0, 0.0)
    assert resolve_fee({"RVN": 0.0}, base_asset="RVN", avg_fill_price=0.003427, log_prefix="test") == (None, 0.0, 0.0)


def test_resolve_fee_unrecognized_asset_returns_no_usd_equivalent():
    """Never guess — an asset that's neither the base nor the quote of
    THIS symbol has no price available from this order's own data."""
    fee_asset, fee_amount, fee_usd = resolve_fee({"BNB": 0.001}, base_asset="RVN", avg_fill_price=0.003427, log_prefix="test")
    assert fee_asset == "BNB"
    assert fee_amount == 0.001
    assert fee_usd is None


def test_resolve_fee_multiple_assets_is_ambiguous_not_summed():
    fee_asset, fee_amount, fee_usd = resolve_fee({"RVN": 1.0, "USDT": 0.01}, base_asset="RVN", avg_fill_price=0.003427, log_prefix="test")
    assert fee_asset is None
    assert fee_amount == 0.0
    assert fee_usd is None


def test_net_base_qty_reduced_when_fee_is_in_base_asset():
    """The exact real-world case: 2917.9 gross - 2.9179 RVN fee = 2914.9821 net."""
    assert net_base_qty_after_fee(2917.9, "RVN", 2.9179, base_asset="RVN") == pytest.approx(2914.9821)


def test_net_base_qty_unaffected_when_fee_is_in_quote_asset():
    assert net_base_qty_after_fee(2917.9, "USDT", 0.01, base_asset="RVN") == 2917.9


def test_net_base_qty_unaffected_when_fee_asset_unknown():
    assert net_base_qty_after_fee(2917.9, None, 0.0, base_asset="RVN") == 2917.9


# ---- compute_max_safe_notional (pure) — FIX 2, 2026-08-24 -----------------
#
# 10 USDT (or whatever the cap is) is a ceiling, never a mandatory size
# — never reject a smaller, genuinely-tradable opportunity.


def test_max_safe_notional_capped_by_available_inventory_below_max():
    """The real RVN scenario: 2914.9821 RVN at a 0.003415 bid is worth
    less than the 10 USDT cap — the safe size must come out below 10,
    not be rejected outright."""
    safe = compute_max_safe_notional(available_base_qty=2914.9821, sell_price=0.003415, max_notional_usdt=10.0, sell_min_notional=1.0, sell_step_size=0.1)
    assert 0 < safe < 10.0


def test_max_safe_notional_capped_by_max_notional_when_inventory_is_abundant():
    safe = compute_max_safe_notional(available_base_qty=1_000_000.0, sell_price=0.003415, max_notional_usdt=10.0, sell_min_notional=1.0, sell_step_size=0.1)
    assert safe == 10.0


def test_max_safe_notional_below_min_notional_returns_zero():
    """Genuinely too little to trade — not a bug, a real floor."""
    safe = compute_max_safe_notional(available_base_qty=10.0, sell_price=0.003415, max_notional_usdt=10.0, sell_min_notional=1.0, sell_step_size=0.1)
    assert safe == 0.0  # 10 RVN * 0.003415 = 0.03415 USDT, under the 1.0 min_notional floor


def test_max_safe_notional_respects_step_size_rounding():
    # 2914.98 RVN rounded down to a 0.1 step is 2914.9, not 2914.98
    safe = compute_max_safe_notional(available_base_qty=2914.98, sell_price=1.0, max_notional_usdt=100_000.0, sell_min_notional=1.0, sell_step_size=0.1)
    assert safe == pytest.approx(2914.9)


def test_max_safe_notional_zero_inventory_is_zero():
    assert compute_max_safe_notional(available_base_qty=0.0, sell_price=0.003415, max_notional_usdt=10.0, sell_min_notional=1.0, sell_step_size=0.1) == 0.0


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


async def test_order_link_id_stays_within_bybits_36_character_limit(monkeypatch):
    """Regression (2026-08-24): the previous f"inventory-{attempt_id}"
    format was 46 characters — 10 over Bybit's documented orderLinkId
    max of 36 — and is the prime suspect for two real retCode=170003
    "unknown parameter" rejections."""
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=2919.7)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.order_client_id is not None
    assert len(result.order_client_id) <= 36
    assert trade.submitted_orders[0][3] == result.order_client_id


async def test_bybit_buy_transmits_the_raw_usdt_notional_not_a_converted_base_qty(monkeypatch):
    """Bybit BUY caller fix (2026-08-24): bybit_live_trade_client.place_
    market_order now sends marketUnit="quoteCoin" for every Buy, meaning
    qty on the wire IS the USDT notional. This caller must pass
    requested_notional_usdt straight through — never pre-convert it to
    an estimated base-asset quantity via a book price first (that
    double-conversion is exactly the caller/client contract mismatch
    this fix closes)."""
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=2919.7)
    await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert len(trade.submitted_orders) == 1
    symbol, side, qty, order_link_id = trade.submitted_orders[0]
    assert side == "Buy"
    assert qty == 10.0  # the raw USDT notional — NOT 10.0 / 0.003420 (an estimated RVN quantity)


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


async def test_fee_in_base_asset_reduces_net_qty_and_is_reported_correctly(monkeypatch):
    """The exact real-world regression (2026-08-24): a fill reporting
    2917.9 RVN gross with a 2.9179 RVN fee must report net_filled_qty
    ~2914.9821 (what the wallet actually holds), fee_asset="RVN", and a
    fee_usd_equivalent around one cent — never $2.9179."""
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=2917.9, fee_asset="RVN", fee_amount=2.9179)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == InventoryConstitutionOutcome.FILLED
    assert result.filled_qty == 2917.9
    assert result.net_filled_qty == pytest.approx(2914.9821)
    assert result.fee_asset == "RVN"
    assert result.fee_amount == 2.9179
    assert result.fee_usd_equivalent == pytest.approx(2.9179 * 0.003420)
    assert result.fee_usd == result.fee_usd_equivalent  # backward-compat alias stays in sync
    assert result.fee_usd < 0.02  # NOT ~$2.92


async def test_barely_short_of_full_required_qty_still_ready_for_a_smaller_arbitrage(monkeypatch):
    """FIX 2 (user directive, 2026-08-24): a fill that falls short of
    compute_required_inventory_qty's own 5%-margin target (as a 10 USDT
    purchase against a 10 USDT-cap target always will) must NOT be
    treated as a failure — max_safe_arbitrage_notional_usdt should come
    back positive and below the cap, and ready_for_arbitrage True,
    exactly the real RVN scenario this fix addresses."""
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=2917.9, fee_asset="RVN", fee_amount=2.9179)
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.net_filled_qty < (result.required_base_qty or 0)  # genuinely short of the full-margin target...
    assert result.ready_for_arbitrage is True  # ...but still usable for a smaller trade
    assert result.max_safe_arbitrage_notional_usdt is not None
    assert 0 < result.max_safe_arbitrage_notional_usdt <= 10.0


async def test_ready_for_arbitrage_false_when_net_qty_cannot_clear_min_notional(monkeypatch):
    monkeypatch.setattr(executor_module, "inventory_guard", _armed_guard())
    trade = FakeBybitTrade(fill_status="Filled", fill_qty=10.0)  # 10 RVN * ~0.0034 << the 1.0 min_order_amt floor
    result = await _executor(bybit_trade=trade).constitute_inventory(SYMBOL, "binance", "bybit", 10.0)
    assert result.max_safe_arbitrage_notional_usdt == 0.0
    assert result.ready_for_arbitrage is False


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
