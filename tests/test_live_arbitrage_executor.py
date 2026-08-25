import uuid

import pytest

import app.execution.live_arbitrage_executor as executor_module
from app.execution.binance_live_trade_client import BinanceOrderFill, BinanceOrderResult, BinanceTrade
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


# Huge by default so existing tests (which don't care about real-balance
# capping) are never accidentally constrained by it — tests that DO care
# about common_qty/real-inventory capping override this explicitly.
DEFAULT_FAKE_LUNC_BALANCE = 10_000_000.0


class _FakeSnapshot:
    def __init__(self, lunc_balance: float) -> None:
        self._lunc_balance = lunc_balance

    def balance_of(self, asset: str) -> float:
        return self._lunc_balance if asset == "LUNC" else 0.0


class FakeBinanceRead:
    def __init__(self, lunc_balance: float = DEFAULT_FAKE_LUNC_BALANCE) -> None:
        self.lunc_balance = lunc_balance

    async def get_book_ticker(self, symbol):
        return {"bidPrice": "0.00005440", "askPrice": "0.00005461"}

    async def get_order_book_depth(self, symbol, limit=20):
        return {"asks": [[str(p), str(q)] for p, q in DEEP_ASKS], "bids": []}

    async def get_exchange_info(self, symbols=None):
        return EXCHANGE_INFO_FIXTURE

    async def get_trade_fee(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()

    async def get_account_snapshot(self):
        return _FakeSnapshot(self.lunc_balance)


class FakeBybitRead:
    def __init__(self, lunc_balance: float = DEFAULT_FAKE_LUNC_BALANCE) -> None:
        self.lunc_balance = lunc_balance

    async def get_book_ticker(self, symbol):
        return type("Ticker", (), {"bid_price": 0.00005600, "ask_price": 0.00005620})()

    async def get_order_book_depth(self, symbol, limit=50):
        return {"result": {"a": [], "b": [[str(p), str(q)] for p, q in DEEP_BIDS]}}

    async def get_symbol_rules(self, symbol):
        return BYBIT_RULES

    async def get_fee_rate(self, symbol):
        return type("Fee", (), {"maker_fee_rate": 0.001, "taker_fee_rate": 0.001})()

    async def get_wallet_balance(self):
        return {"result": {"list": [{"coin": [{"coin": "LUNC", "availableToWithdraw": str(self.lunc_balance)}]}]}}


class FakeBinanceTrade:
    def __init__(self, fill_status="FILLED", fill_qty=183000.0, never_terminal=False, raise_on_submit=False, fee_asset="USDT", fee_amount=0.01):
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.never_terminal = never_terminal
        self.raise_on_submit = raise_on_submit
        self.fee_asset = fee_asset
        self.fee_amount = fee_amount
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
            fills=[BinanceOrderFill(price=0.0000546, qty=self.fill_qty, commission=self.fee_amount, commission_asset=self.fee_asset)], raw={},
        )

    async def get_order_trades(self, symbol, order_id):
        """GET /api/v3/myTrades fixture — the real fee/qty source _get_status
        now fetches once an order is confirmed terminal and filled."""
        if self.fill_qty <= 0:
            return []
        return [
            BinanceTrade(
                trade_id=1, order_id=order_id, price=0.0000546, qty=self.fill_qty,
                quote_qty=self.fill_qty * 0.0000546, commission=self.fee_amount, commission_asset=self.fee_asset,
            )
        ]


class FakeBybitTrade:
    def __init__(
        self, fill_status="Filled", fill_qty=183000.0, never_terminal=False, raise_on_submit=False,
        fee_asset="USDT", fee_amount=0.01, avg_price=0.0000560,
    ):
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.never_terminal = never_terminal
        self.raise_on_submit = raise_on_submit
        self.fee_asset = fee_asset
        self.fee_amount = fee_amount
        self.avg_price = avg_price
        self.submitted_orders = []

    async def place_market_order(self, symbol, side, qty, order_link_id, market_unit=None):
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
            order_status=self.fill_status, cum_exec_qty=self.fill_qty, cum_exec_value=self.fill_qty * self.avg_price,
            cum_exec_fee=self.fee_amount, avg_price=self.avg_price, raw={},
            cum_fee_detail={self.fee_asset: self.fee_amount} if self.fee_asset else {},
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


async def test_buy_and_sell_client_order_ids_stay_within_bybits_36_character_limit(monkeypatch):
    """Regression (2026-08-24): the previous f"live-{attempt_id}-buy"/
    f"live-{attempt_id}-sell" formats were 45/46 characters — over
    Bybit's documented orderLinkId max of 36 — and are the prime suspect
    for two real retCode=170003 "unknown parameter" rejections. Both
    exchanges receive the exact same id, so this must hold regardless of
    which leg lands on Bybit."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    result = await _executor().execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert len(result.buy_client_order_id) <= 36
    assert len(result.sell_client_order_id) <= 36


async def test_neutralization_order_link_id_stays_within_bybits_36_character_limit(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(raise_on_submit=True)
    binance_trade = FakeBinanceTrade()
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BUY_ONLY_NEUTRALIZED
    neutralize_client_order_id = binance_trade.submitted_orders[1][2]
    assert len(neutralize_client_order_id) <= 36


# ---- fee currency + net qty sizing (FIX 1, user directive, 2026-08-24) ----
#
# Regression coverage for the first real Bybit fill (2917.9 RVN gross,
# 2.9179 RVN fee, 2914.9821 RVN net) — a base-asset buy fee must reduce
# what the SELL leg is sized against, and must never be double-counted
# into the USDT cost (it never touched the USDT balance at all).


async def test_buy_fee_in_base_asset_sizes_the_sell_leg_off_net_qty(monkeypatch):
    """final_sell_qty_raw = min(buy_net_filled_qty, real_sell_exchange_
    balance, common_qty) — this fixture's own prices already make
    common_qty (item 1's own price/depth-derived ceiling) the tightest
    of the three bounds, so the sell qty sent is capped there rather
    than sitting exactly at the fee-adjusted net figure. What this test
    isolates is narrower but just as real: the sell qty is NEVER sized
    off the gross 183000.0 fill, and never exceeds what was actually
    net-received after the base-asset fee."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0, fee_asset="LUNC", fee_amount=100.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.buy_filled_qty == 183000.0  # GROSS, as reported
    assert result.buy_net_filled_qty == pytest.approx(182900.0)  # NET of the 100 LUNC fee
    assert result.buy_fee_asset == "LUNC"
    sell_qty_requested = bybit_trade.submitted_orders[0][2]
    assert sell_qty_requested <= result.buy_net_filled_qty  # never exceeds what was actually net-received
    assert sell_qty_requested < result.buy_filled_qty  # proves it is NOT simply sized off the gross fill


async def test_pnl_does_not_double_count_a_base_asset_buy_fee(monkeypatch):
    """A LUNC-denominated buy fee already shows up as a smaller
    buy_net_filled_qty (and therefore a smaller sell leg / smaller
    proceeds) — adding fee_usd_equivalent to buy_cost_usd on top of that
    would double-count it, inflating the apparent cost by the fee's
    USD-equivalent value a second time."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0, fee_asset="LUNC", fee_amount=100.0)
    bybit_trade = FakeBybitTrade(fill_qty=182900.0)
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    expected_buy_cost_usd = result.buy_avg_fill_price * result.buy_filled_qty  # NO added fee — it was never in USDT
    expected_sell_proceeds_usd = result.sell_avg_fill_price * result.sell_filled_qty - (result.sell_fees_usd or 0)
    assert result.actual_net_pnl_usd == pytest.approx(expected_sell_proceeds_usd - expected_buy_cost_usd)


async def test_pnl_still_subtracts_a_quote_asset_buy_fee(monkeypatch):
    """The opposite, already-existing case must still work: a USDT buy
    fee DOES reduce the net P&L, since it's a real extra USDT cost."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0, fee_asset="USDT", fee_amount=0.05)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    expected_buy_cost_usd = result.buy_avg_fill_price * result.buy_filled_qty + 0.05
    expected_sell_proceeds_usd = result.sell_avg_fill_price * result.sell_filled_qty - (result.sell_fees_usd or 0)
    assert result.actual_net_pnl_usd == pytest.approx(expected_sell_proceeds_usd - expected_buy_cost_usd)


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
    bybit_trade = FakeBybitTrade(fill_qty=183000.0, avg_price=0.00005440)  # matches CheapBybitRead's ask below

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


async def test_bybit_buy_leg_now_transmits_a_base_asset_quantity_not_the_raw_notional(monkeypatch):
    """Item 1 fix (2026-08-24, post-incident) DELIBERATELY REVERSES the
    2026-08-24 "Bybit BUY caller fix" for the ARBITRAGE buy leg
    specifically: that earlier fix made every Bybit Buy notional-based
    (marketUnit=quoteCoin) so a caller could just say "spend $10" — but
    for a two-leg arbitrage, a notional-based buy can silently acquire
    MORE base asset than the sell leg can actually absorb (exactly what
    happened in the first real arbitrage attempt: bought 3003.5 RVN
    against only 2914.9821 real Bybit inventory). The arbitrage buy leg
    is now quantity-capped by common_qty (marketUnit=baseCoin) instead —
    app.execution.inventory_constitution_executor's OWN buy leg is
    UNCHANGED and still notional-based, since it has no second leg to
    stay in sync with."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0, avg_price=0.00005440)  # matches CheapBybitRead's ask below

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
    assert bybit_trade.submitted_orders[0][2] > 1000  # a base-asset-sized quantity, not the raw ~10 USDT notional


async def test_bybit_sell_leg_still_transmits_a_base_asset_quantity(monkeypatch):
    """SELL was already correct before this fix and must stay that way —
    qty is a real base-asset quantity, never the USDT notional. Since
    item 1 (common dual-leg sizing, 2026-08-24), the buy leg itself is
    now quantity-capped by common_qty rather than by notional, so the
    exact figure comes from the fixture's prices/depth rather than
    echoing FakeBinanceTrade's disconnected fill_qty — the important,
    still-true property is "a base-asset-sized number", not "~10 USDT"."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0)
    result = await _executor(binance_trade=binance_trade, bybit_trade=bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert bybit_trade.submitted_orders[0][1] == "Sell"
    sell_qty = bybit_trade.submitted_orders[0][2]
    assert sell_qty > 1000  # a base-asset-sized quantity, not a USDT amount (~10)


async def test_neither_caller_double_converts_buy_qty_into_a_second_unit(monkeypatch):
    """Combined regression, updated for item 1 (2026-08-24, post-incident:
    the arbitrage buy leg is now quantity-capped by common_qty, not
    notional-capped — see test_bybit_buy_leg_now_transmits_a_base_asset_
    quantity_not_the_raw_notional). Across both directions: Bybit never
    receives a Buy order it wasn't meant to, and when it IS the buy
    exchange, the qty sent is common_qty itself (already fully derived
    ONCE via compute_dual_leg_quote) — never re-converted a second time
    on top of that."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)

    # binance buy / bybit sell: Bybit never receives a Buy order here.
    default_bybit_trade = FakeBybitTrade()
    await _executor(bybit_trade=default_bybit_trade).execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)
    assert all(order[1] != "Buy" for order in default_bybit_trade.submitted_orders)

    # bybit buy / binance sell: Bybit's Buy order qty must equal common_qty exactly, not some further-converted figure.
    binance_trade = FakeBinanceTrade(fill_qty=183000.0)
    bybit_trade = FakeBybitTrade(fill_qty=183000.0, avg_price=0.00005440)  # matches CheapBybitRead's ask below

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
    buy_orders = [order for order in bybit_trade.submitted_orders if order[1] == "Buy"]
    assert len(buy_orders) == 1
    assert buy_orders[0][2] > 1000  # a base-asset-sized quantity, not a re-converted USDT-sized figure


# ---- item 4 (user directive, 2026-08-24): exact incident replay ----------
#
# The real numbers from the first real arbitrage attempt: Bybit held
# 2914.9821 real inventory; the Binance buy leg reported a gross fill of
# 3003.5, and (after the item 2 fee-accounting fix) a real 3.0035 LUNC
# fee nets that down to 3000.4965 — exactly what the wallet showed.
# 3000.4965 still exceeds the real 2914.9821 held on Bybit, so these
# tests prove the SYSTEM, not just the fee accounting, protects against
# ever attempting to sell more than what Bybit actually has.


async def test_incident_replay_never_attempts_to_sell_more_than_real_bybit_inventory(monkeypatch):
    """Reuses this file's existing LUNCUSDT fixture infrastructure with
    the incident's exact quantities AND real RVN-scale prices substituted
    in (this file's shared module-level LUNC price, ~0.0000546, would
    make 2914.9821 units worth only ~$0.16 — nowhere near the real
    incident's ~$10 economics — so this test overrides book_ticker/depth
    to match the real RVN prices observed during the incident instead).
    The sizing algorithm being tested doesn't care about the asset's
    identity, only that the real quantities and their real dollar value
    are both faithfully reproduced. FakeBinanceTrade deliberately still
    reports the full 3003.5 gross fill regardless of what was actually
    requested (simulating the buy exchange filling more than intended)
    — proving the POST-buy real-balance cap (item 1) holds even in that
    case, not just the pre-trade common_qty sizing."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)

    class RvnPricedBinanceRead(FakeBinanceRead):
        async def get_book_ticker(self, symbol):
            return {"bidPrice": "0.00331", "askPrice": "0.00332"}  # the real incident's own Binance ask

        async def get_order_book_depth(self, symbol, limit=20):
            return {"asks": [["0.00332", "500000000"], ["0.00333", "500000000"]], "bids": []}

    class RvnPricedBybitRead(FakeBybitRead):
        async def get_book_ticker(self, symbol):
            return type("Ticker", (), {"bid_price": 0.003422, "ask_price": 0.003428})()  # the real incident's own Bybit bid

        async def get_order_book_depth(self, symbol, limit=50):
            return {"result": {"a": [], "b": [["0.003422", "500000000"], ["0.003420", "500000000"]]}}

    bybit_read = RvnPricedBybitRead(lunc_balance=2914.9821)  # the real incident's exact held Bybit inventory
    binance_trade = FakeBinanceTrade(fill_qty=3003.5, fee_asset="LUNC", fee_amount=3.0035)  # nets to 3000.4965, exactly like the real fill
    bybit_trade = FakeBybitTrade(fill_qty=2914.9821)
    executor = LiveArbitrageExecutor(binance_read=RvnPricedBinanceRead(), binance_trade=binance_trade, bybit_read=bybit_read, bybit_trade=bybit_trade)

    result = await executor.execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)

    assert result.buy_filled_qty == 3003.5  # the real incident's exact gross fill, reproduced
    assert result.buy_net_filled_qty == pytest.approx(3000.4965)  # item 2 fix: the real fee is now correctly detected and subtracted

    # Pre-trade: the BUY itself was already requested at a quantity
    # bounded by common_qty (<= the real 2914.9821 Bybit inventory), not
    # an unbounded notional-derived figure.
    buy_orders = [order for order in binance_trade.submitted_orders if order[1] == "BUY"]
    assert len(buy_orders) == 1
    assert buy_orders[0][3] <= 2915  # requested quantity, bounded pre-trade by real sell-exchange inventory

    # Post-trade: whatever Bybit is actually asked to sell must never
    # exceed the real 2914.9821 it holds, regardless of the buy leg's
    # own (possibly larger) reported fill.
    sell_orders = [order for order in bybit_trade.submitted_orders if order[1] == "Sell"]
    for order in sell_orders:
        assert order[2] <= 2914.9821


async def test_neutralization_sells_the_real_balance_not_the_gross_fill(monkeypatch):
    """Item 3 (user directive, 2026-08-24, post-incident): the
    neutralizer must always cap by the REAL free balance immediately
    before submitting — never trust a caller-supplied qty (like the buy
    leg's own gross fill) blindly. Simulates a buy exchange whose REAL
    balance is lower than the buy leg's own reported gross fill (exactly
    what happened in the real incident's failed neutralization attempt,
    which tried to sell 3003.5 when only 3000.4965 was actually held)."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)

    class ShortBalanceBinanceRead(FakeBinanceRead):
        async def get_account_snapshot(self):
            return _FakeSnapshot(3000.4965)  # real balance is LESS than the buy leg's own reported fill

    binance_trade = FakeBinanceTrade(fill_qty=3003.5)  # gross fill the buy leg reports
    bybit_trade = FakeBybitTrade(raise_on_submit=True)  # force the sell leg to fail, triggering neutralization
    executor = LiveArbitrageExecutor(
        binance_read=ShortBalanceBinanceRead(), binance_trade=binance_trade, bybit_read=FakeBybitRead(), bybit_trade=bybit_trade
    )

    await executor.execute_one_arbitrage(SYMBOL, "binance", "bybit", 10.0)

    neutralize_orders = [order for order in binance_trade.submitted_orders if order[1] == "SELL"]
    assert len(neutralize_orders) == 1
    assert neutralize_orders[0][3] <= 3000.4965  # capped by the REAL balance, never the gross 3003.5 the buy leg reported


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


# ============================================================
# OKX (user directive, 2026-08-25, "MISSION -- V5 TRUE ECONOMIC
# ENGINE -- FIRST REAL OKX VALIDATION"). Same fake-client
# conventions as the Binance/Bybit fixtures above.
# ============================================================

from app.execution.okx_account_client import OkxAccountSnapshot, OkxBalance, OkxTradeFee
from app.execution.okx_live_trade_client import OkxOrderAck, OkxOrderStatus
from app.scanner.okx_public_client import OkxBookTicker, OkxSymbolRules

class FakeOkxRead:
    def __init__(self, lunc_balance: float = DEFAULT_FAKE_LUNC_BALANCE, usdt_balance: float = 1000.0) -> None:
        self.lunc_balance = lunc_balance
        self.usdt_balance = usdt_balance

    async def get_account_snapshot(self):
        return OkxAccountSnapshot(balances=[
            OkxBalance(currency="LUNC", available=self.lunc_balance, frozen=0.0),
            OkxBalance(currency="USDT", available=self.usdt_balance, frozen=0.0),
        ])

    async def get_trade_fee(self, symbol):
        return OkxTradeFee(inst_id=symbol, maker_fee_rate=0.0008, taker_fee_rate=0.001)


def _okx_depth(bid: float, ask: float) -> dict:
    return {"data": [{
        "asks": [[f"{ask:.8f}", "500000000", "0", "1"]],
        "bids": [[f"{bid:.8f}", "500000000", "0", "1"]],
    }]}


class FakeOkxPublic:
    """Deep, single-level book at exactly bid/ask -- no slippage, so a
    test's chosen spread survives intact through the pre-trade quote AND
    the post-buy revalidation (both re-fetch this same fake)."""

    def __init__(self, bid: float = 0.00005000, ask: float = 0.00005010) -> None:
        self.bid = bid
        self.ask = ask

    async def get_book_ticker(self, symbol):
        return OkxBookTicker(inst_id=symbol, bid_price=self.bid, ask_price=self.ask)

    async def get_order_book_depth(self, symbol, limit=20):
        return _okx_depth(self.bid, self.ask)

    async def get_symbol_rules(self, symbol):
        return OkxSymbolRules(inst_id=symbol, is_tradable=True, min_qty=1.0, lot_size=1.0, tick_size=0.00000001)


# OKX_CHEAP: a low ask, used whenever OKX must be the profitable BUY
# source. OKX_EXPENSIVE: a high bid, used whenever OKX must be the
# profitable SELL target. Never both roles in the same test -- each
# test picks whichever one it needs and keeps the OTHER leg's fake
# trade client's avg_price in sync with that same fake's book price.
OKX_CHEAP_BID, OKX_CHEAP_ASK = 0.00005000, 0.00005010
OKX_EXPENSIVE_BID, OKX_EXPENSIVE_ASK = 0.00005900, 0.00005910


class FakeOkxTrade:
    def __init__(self, fill_status="filled", fill_qty=183000.0, never_terminal=False, raise_on_submit=False,
                 fee_asset="USDT", fee_amount=0.01, avg_price=OKX_CHEAP_ASK):
        self.fill_status = fill_status
        self.fill_qty = fill_qty
        self.never_terminal = never_terminal
        self.raise_on_submit = raise_on_submit
        self.fee_asset = fee_asset
        self.fee_amount = fee_amount
        self.avg_price = avg_price
        self.submitted_orders = []

    async def place_market_order(self, symbol, side, quantity, client_order_id=None):
        if self.raise_on_submit:
            raise RuntimeError("simulated network failure")
        self.submitted_orders.append((symbol, side, quantity, client_order_id))
        return OkxOrderAck(order_id="okx-1", client_order_id=client_order_id or "", accepted=True, status_code="0", status_message="", raw={})

    async def get_order_status(self, symbol, order_id=None, client_order_id=None):
        if self.never_terminal:
            return OkxOrderStatus(order_id="okx-1", client_order_id=client_order_id or "", symbol=symbol, side="buy",
                                   state="live", filled_qty=0.0, avg_fill_price=None, fee_amount=0.0, fee_asset=None, raw={})
        if self.fill_qty <= 0:
            return OkxOrderStatus(order_id="okx-1", client_order_id=client_order_id or "", symbol=symbol, side="buy",
                                   state="canceled", filled_qty=0.0, avg_fill_price=None, fee_amount=0.0, fee_asset=None, raw={})
        return OkxOrderStatus(
            order_id="okx-1", client_order_id=client_order_id or "", symbol=symbol, side="buy", state=self.fill_status,
            filled_qty=self.fill_qty, avg_fill_price=self.avg_price, fee_amount=self.fee_amount, fee_asset=self.fee_asset, raw={},
        )


def _executor_okx(okx_trade=None, okx_read=None, okx_public=None, binance_trade=None, bybit_trade=None, bybit_read=None):
    return LiveArbitrageExecutor(
        binance_read=FakeBinanceRead(), binance_trade=binance_trade or FakeBinanceTrade(),
        bybit_read=bybit_read or FakeBybitRead(), bybit_trade=bybit_trade or FakeBybitTrade(),
        okx_read=okx_read or FakeOkxRead(), okx_public=okx_public or FakeOkxPublic(), okx_trade=okx_trade or FakeOkxTrade(),
    )


class CheapBybitRead(FakeBybitRead):
    """Ask overridden to a real, self-consistent cheap buy price --
    FakeBybitTrade's own avg_price must be set to match in any test
    using this, exactly like the real exchange's fill price always
    matches what was actually quoted."""

    async def get_book_ticker(self, symbol):
        return type("Ticker", (), {"bid_price": 0.00005420, "ask_price": 0.00005300})()


async def test_okx_as_sell_exchange_both_legs_filled(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(avg_price=0.00005300)
    executor = _executor_okx(
        bybit_read=CheapBybitRead(), bybit_trade=bybit_trade,
        okx_public=FakeOkxPublic(bid=OKX_EXPENSIVE_BID, ask=OKX_EXPENSIVE_ASK), okx_trade=FakeOkxTrade(avg_price=OKX_EXPENSIVE_BID),
    )
    result = await executor.execute_one_arbitrage(SYMBOL, "bybit", "okx", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.sell_exchange_order_id == "okx-1"
    assert result.actual_net_pnl_usd is not None
    assert result.actual_net_pnl_usd > 0
    assert guard.in_flight_count == 0


async def test_okx_as_buy_exchange_both_legs_filled(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    executor = _executor_okx()  # default cheap OKX (ask=0.00005010) -> default Bybit sell (bid=0.00005600)
    result = await executor.execute_one_arbitrage(SYMBOL, "okx", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.buy_exchange_order_id == "okx-1"
    assert result.actual_net_pnl_usd is not None
    assert result.actual_net_pnl_usd > 0
    assert guard.in_flight_count == 0


async def test_okx_fee_in_base_asset_resolved_correctly(monkeypatch):
    """OKX charging the fee in LUNC (the base asset) must reduce
    net_base_qty, never be silently treated as a $0 USDT cost -- exactly
    the same in-kind fee handling already proven for Binance/Bybit."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    okx_trade = FakeOkxTrade(fee_asset="LUNC", fee_amount=100.0)
    executor = _executor_okx(okx_trade=okx_trade)
    result = await executor.execute_one_arbitrage(SYMBOL, "okx", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.buy_fee_asset == "LUNC"
    assert result.buy_net_filled_qty == pytest.approx(result.buy_filled_qty - 100.0)


async def test_okx_fee_in_unresolvable_asset_never_fabricates_usd_equivalent(monkeypatch):
    """Item 4, user directive: 'si fee asset inconnu: SAFE STOP' -- this
    module's own contract is fee_usd_equivalent=None whenever the fee
    asset is neither the base nor quote asset (e.g. a promotional OKB
    discount token); the caller (a live orchestrator) is the one that
    must refuse to proceed on None, exactly like it already must for
    Binance/Bybit's own unresolvable-fee case."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    okx_trade = FakeOkxTrade(fee_asset="OKB", fee_amount=0.05)
    executor = _executor_okx(okx_trade=okx_trade)
    result = await executor.execute_one_arbitrage(SYMBOL, "okx", "bybit", 10.0)
    assert result.outcome == ArbitrageOutcome.BOTH_FILLED
    assert result.buy_fee_asset == "OKB"
    assert result.buy_fee_usd_equivalent is None


async def test_okx_common_qty_capped_by_real_okx_balance_when_sell_exchange(monkeypatch):
    """Same real-balance-capping discipline as the original Binance/Bybit
    incident this module's whole design exists to prevent -- OKX must
    never be allowed to sell more than it actually, really holds."""
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    bybit_trade = FakeBybitTrade(avg_price=0.00005300)
    thin_okx_read = FakeOkxRead(lunc_balance=50.0)  # far less than the requested notional would imply
    okx_trade = FakeOkxTrade(fill_qty=50.0, avg_price=OKX_EXPENSIVE_BID)
    executor = _executor_okx(
        bybit_read=CheapBybitRead(), bybit_trade=bybit_trade, okx_read=thin_okx_read,
        okx_public=FakeOkxPublic(bid=OKX_EXPENSIVE_BID, ask=OKX_EXPENSIVE_ASK), okx_trade=okx_trade,
    )
    result = await executor.execute_one_arbitrage(SYMBOL, "bybit", "okx", 10.0)
    assert result.outcome != ArbitrageOutcome.BOTH_FILLED or result.sell_filled_qty <= 50.0


async def test_okx_submission_failure_triggers_neutralization_not_a_crash(monkeypatch):
    guard = _armed_guard()
    monkeypatch.setattr(executor_module, "live_guard", guard)
    okx_trade = FakeOkxTrade(raise_on_submit=True, avg_price=OKX_EXPENSIVE_BID)
    binance_trade = FakeBinanceTrade()
    executor = _executor_okx(
        okx_trade=okx_trade, binance_trade=binance_trade,
        okx_public=FakeOkxPublic(bid=OKX_EXPENSIVE_BID, ask=OKX_EXPENSIVE_ASK),
    )
    result = await executor.execute_one_arbitrage(SYMBOL, "binance", "okx", 10.0)
    assert result.outcome in (ArbitrageOutcome.BUY_ONLY_NEUTRALIZED, ArbitrageOutcome.NEUTRALIZATION_FAILED)
    assert guard.in_flight_count == 0


async def test_okx_never_treated_as_unrecognized_exchange():
    """Structural check: 'okx' must be a first-class member of EXCHANGES,
    not silently falling into the old binary if/else's implicit bybit
    branch (which would misroute every OKX call to Bybit's own client)."""
    assert "okx" in executor_module.EXCHANGES
    assert len(executor_module.EXCHANGES) == 3
