from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from app.reporting.real_trading_summary import compute_real_trading_summary


@dataclass
class _FakeRecord:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    outcome: str
    actual_net_pnl_usd: float | None
    buy_fees_usd: float | None
    sell_fees_usd: float | None
    started_at: datetime


def _row(pnl, hour=10, minute=0, fees=(0.01, 0.01)):
    return _FakeRecord(
        symbol="ZRO/USDT", buy_exchange="binance", sell_exchange="bybit", outcome="both_filled",
        actual_net_pnl_usd=pnl, buy_fees_usd=fees[0], sell_fees_usd=fees[1],
        started_at=datetime(2026, 8, 23, hour, minute, tzinfo=UTC).replace(tzinfo=None),
    )


TODAY_START = datetime(2026, 8, 23, 0, 0, tzinfo=UTC).replace(tzinfo=None)
NOW = datetime(2026, 8, 23, 23, 0, tzinfo=UTC).replace(tzinfo=None)


def test_empty_rows_produce_safe_defaults():
    summary = compute_real_trading_summary([], now=NOW, today_start=TODAY_START)
    assert summary.total_real_trades == 0
    assert summary.win_rate_pct is None
    assert summary.average_profit_per_trade_usd is None
    assert summary.today_real_pnl_usd == 0.0


def test_win_loss_and_win_rate():
    rows = [_row(0.05), _row(0.03), _row(-0.02)]
    summary = compute_real_trading_summary(rows, now=NOW, today_start=TODAY_START)
    assert summary.wins == 2
    assert summary.losses == 1
    assert summary.win_rate_pct == 2 / 3 * 100


def test_total_and_today_pnl():
    rows = [_row(0.05, hour=10), _row(0.03, hour=14)]
    summary = compute_real_trading_summary(rows, now=NOW, today_start=TODAY_START)
    assert summary.total_real_pnl_usd == 0.08
    assert summary.today_real_pnl_usd == 0.08  # all rows are "today" in this fixture


def test_yesterday_trades_excluded_from_today_pnl():
    yesterday = _row(0.05, hour=10)
    yesterday.started_at = datetime(2026, 8, 22, 10, 0, tzinfo=UTC).replace(tzinfo=None)
    today = _row(0.03, hour=10)
    summary = compute_real_trading_summary([yesterday, today], now=NOW, today_start=TODAY_START)
    assert summary.today_real_pnl_usd == 0.03
    assert summary.total_real_pnl_usd == 0.08


def test_total_fees_sums_both_legs():
    rows = [_row(0.05, fees=(0.01, 0.02)), _row(0.03, fees=(0.015, 0.005))]
    summary = compute_real_trading_summary(rows, now=NOW, today_start=TODAY_START)
    assert summary.total_real_fees_usd == pytest.approx(0.01 + 0.02 + 0.015 + 0.005)


def test_last_trades_sorted_most_recent_first_and_capped():
    rows = [_row(0.01, hour=h) for h in range(1, 15)]
    summary = compute_real_trading_summary(rows, now=NOW, today_start=TODAY_START, last_n=5)
    assert len(summary.last_trades) == 5
    assert summary.last_trades[0].started_at.hour == 14
    assert summary.last_trades[-1].started_at.hour == 10
