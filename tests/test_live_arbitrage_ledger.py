import uuid

import pytest

from app.database.repository import save_live_arbitrage_execution
from app.execution.live_arbitrage_executor import ArbitrageOutcome, LiveArbitrageResult


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


async def test_save_live_arbitrage_execution_persists_both_filled_outcome():
    result = LiveArbitrageResult(
        attempt_id=uuid.uuid4(),
        symbol="LUNCUSDT",
        buy_exchange="binance",
        sell_exchange="bybit",
        outcome=ArbitrageOutcome.BOTH_FILLED,
        reason=None,
        predicted_net_profit_usd=0.05,
        predicted_fees_usd=0.02,
        predicted_slippage_pct=0.01,
        safety_adjusted_predicted_profit_usd=0.04,
        buy_client_order_id="live-x-buy",
        buy_exchange_order_id="123",
        buy_status="FILLED",
        buy_filled_qty=183000.0,
        buy_avg_fill_price=0.0000546,
        buy_fees_usd=0.01,
        buy_submitted_at=100.0,
        buy_confirmed_at=100.3,
        sell_client_order_id="live-x-sell",
        sell_exchange_order_id="abc",
        sell_status="Filled",
        sell_filled_qty=183000.0,
        sell_avg_fill_price=0.0000560,
        sell_fees_usd=0.01,
        sell_submitted_at=100.4,
        sell_confirmed_at=100.9,
        actual_net_pnl_usd=0.046,
        prediction_error_usd=-0.004,
        started_at=99.9,
        completed_at=101.0,
    )
    session = FakeSession()
    record = await save_live_arbitrage_execution(session, result)

    assert session.added == [record]
    assert record.outcome == "both_filled"
    assert record.buy_exchange == "binance"
    assert record.sell_exchange == "bybit"
    assert record.buy_latency_ms == pytest.approx(300.0)
    assert record.sell_latency_ms == pytest.approx(500.0)
    assert record.actual_realized_spread_pct is not None and record.actual_realized_spread_pct > 0
    assert record.actual_net_pnl_usd == 0.046


async def test_save_live_arbitrage_execution_persists_refused_outcome_with_no_orders():
    """The ledger must record a NO_TRADE_REFUSED attempt too — no
    silent gap just because no order was ever submitted."""
    result = LiveArbitrageResult(
        attempt_id=uuid.uuid4(), symbol="LUNCUSDT", buy_exchange="binance", sell_exchange="bybit",
        outcome=ArbitrageOutcome.NO_TRADE_REFUSED, reason="live_trading_enabled is False",
    )
    session = FakeSession()
    record = await save_live_arbitrage_execution(session, result)
    assert record.outcome == "no_trade_refused"
    assert record.buy_client_order_id is None
    assert record.buy_latency_ms is None
