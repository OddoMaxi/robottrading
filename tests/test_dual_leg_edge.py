from dataclasses import dataclass
from datetime import UTC, datetime

from app.reporting.dual_leg_edge import _direction_stats, _rejection_bucket, recommend_safety_margin_usd, safety_adjusted_profit_usd


@dataclass
class _FakeRow:
    symbol: str = "LUNCUSDT"
    buy_exchange: str = "binance"
    sell_exchange: str = "bybit"
    observed_at: datetime = datetime(2026, 8, 23, 8, 0, tzinfo=UTC)
    net_profit_usd: float = 0.05
    buy_fee_source: str = "real_account_fee"
    sell_fee_source: str = "real_account_fee"
    buy_tradable: bool = True
    sell_tradable: bool = True
    buy_lot_size_pass: bool = True
    sell_lot_size_pass: bool = True
    buy_min_notional_pass: bool = True
    sell_min_notional_pass: bool = True
    executable: bool = True


def test_rejection_bucket_prioritizes_tradability_first():
    row = _FakeRow(buy_tradable=False)
    assert _rejection_bucket(row) == "buy_leg_not_tradable"


def test_rejection_bucket_falls_through_to_net_profit():
    row = _FakeRow(net_profit_usd=-0.01)
    assert _rejection_bucket(row) == "net_profit_leq_zero"


def test_rejection_bucket_lot_size_before_min_notional():
    row = _FakeRow(sell_lot_size_pass=False)
    assert _rejection_bucket(row) == "sell_lot_size"


def test_recommend_safety_margin_is_stdev():
    import statistics

    values = [0.05, 0.06, 0.04, 0.055]
    assert recommend_safety_margin_usd(values) == round(statistics.pstdev(values), 6)


def test_safety_adjusted_profit_subtracts_margin():
    assert safety_adjusted_profit_usd(0.05, 0.01) == 0.04


def test_direction_stats_groups_by_buy_sell_exchange_pair():
    rows = [
        _FakeRow(buy_exchange="binance", sell_exchange="bybit", net_profit_usd=0.05),
        _FakeRow(buy_exchange="binance", sell_exchange="bybit", net_profit_usd=0.06),
        _FakeRow(buy_exchange="bybit", sell_exchange="binance", net_profit_usd=0.02),
    ]
    stats = _direction_stats(rows)
    by_direction = {s.direction: s for s in stats}
    assert by_direction["binance_to_bybit"].observations == 2
    assert by_direction["bybit_to_binance"].observations == 1
    assert by_direction["binance_to_bybit"].real_fee_coverage_pct == 100.0
