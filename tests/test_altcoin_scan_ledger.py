import uuid
from datetime import UTC, datetime

from app.database.repository import save_altcoin_scan_observation
from app.execution.dual_leg_quote import DualLegQuote
from app.scanner.cross_exchange_scanner import DirectionQuote


class FakeSession:
    def __init__(self) -> None:
        self.added: list = []

    def add(self, obj) -> None:
        self.added.append(obj)

    async def flush(self) -> None:
        pass


def _quote(**overrides) -> DualLegQuote:
    base = dict(
        opportunity_id=uuid.uuid4(),
        symbol="ZROUSDT",
        buy_exchange="binance",
        sell_exchange="bybit",
        buy_execution_price=3.10,
        sell_execution_price=3.16,
        executable_qty=322.0,
        buy_valid_qty=322.0,
        sell_valid_qty=322.0,
        gross_spread_pct=1.9,
        buy_fee_usd=1.0,
        sell_fee_usd=1.0,
        buy_slippage_pct=0.01,
        sell_slippage_pct=0.01,
        buy_quote_age_ms=10.0,
        sell_quote_age_ms=10.0,
        dual_leg_latency_ms=200.0,
        net_profit_usd=15.0,
        net_return_bps=150.0,
        buy_min_notional_pass=True,
        buy_lot_size_pass=True,
        sell_min_notional_pass=True,
        sell_lot_size_pass=True,
        buy_tradable=True,
        sell_tradable=True,
        executable=True,
        reason=None,
        buy_fee_source="real_account_fee",
        sell_fee_source="real_account_fee",
        computed_at=100.0,
    )
    base.update(overrides)
    return DualLegQuote(**base)


async def test_save_altcoin_scan_observation_computes_net_profit_per_1000usdt():
    dq = DirectionQuote(symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="bybit", quote=_quote())
    session = FakeSession()
    record = await save_altcoin_scan_observation(
        session, dq, observed_at=datetime(2026, 8, 23, 18, 0, tzinfo=UTC).replace(tzinfo=None), continuity_status="new", persistence_seconds=0.0
    )
    assert session.added == [record]
    assert record.symbol == "ZRO/USDT"
    assert record.executable is True
    assert record.net_profit_usd == 15.0
    reference_notional = 322.0 * 3.10
    expected_per_1000 = 15.0 / reference_notional * 1000
    assert record.net_profit_per_1000usdt == expected_per_1000


async def test_save_altcoin_scan_observation_handles_zero_executable_qty():
    """No fill possible (rejected) must not divide by zero."""
    dq = DirectionQuote(
        symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="okx",
        quote=_quote(executable_qty=0.0, buy_valid_qty=0.0, sell_valid_qty=0.0, executable=False, reason="below min notional", net_profit_usd=0.0),
    )
    session = FakeSession()
    record = await save_altcoin_scan_observation(
        session, dq, observed_at=datetime(2026, 8, 23, 18, 0, tzinfo=UTC).replace(tzinfo=None), continuity_status="none", persistence_seconds=0.0
    )
    assert record.net_profit_per_1000usdt == 0.0
    assert record.executable is False
    assert record.rejection_reason == "below min notional"
