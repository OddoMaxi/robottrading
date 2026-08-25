from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from app.reporting.live_trading_dashboard import (
    compute_cost_basis_by_asset_exchange,
    compute_inventory_constitution_summary,
    compute_inventory_position_status,
    compute_missed_opportunity_causes,
    compute_real_pnl_breakdown,
    compute_trade_counts,
)


def _arb_row(
    outcome="both_filled", symbol="RVNUSDT", buy_exchange="binance", sell_exchange="bybit", started_at=None,
    actual_net_pnl_usd=0.2, predicted_net_profit_usd=0.21, buy_filled_qty=2000.0, buy_net_filled_qty=1995.0,
    buy_avg_fill_price=0.0033, sell_filled_qty=1990.0, sell_avg_fill_price=0.00335,
    buy_fees_usd=0.01, sell_fees_usd=0.01, buy_latency_ms=1000.0, sell_latency_ms=1200.0,
):
    return SimpleNamespace(
        outcome=outcome, symbol=symbol, buy_exchange=buy_exchange, sell_exchange=sell_exchange,
        started_at=started_at or datetime(2026, 8, 25, 10, 0, 0, tzinfo=UTC),
        actual_net_pnl_usd=actual_net_pnl_usd, predicted_net_profit_usd=predicted_net_profit_usd,
        buy_filled_qty=buy_filled_qty, buy_net_filled_qty=buy_net_filled_qty, buy_avg_fill_price=buy_avg_fill_price,
        sell_filled_qty=sell_filled_qty, sell_avg_fill_price=sell_avg_fill_price,
        buy_fees_usd=buy_fees_usd, sell_fees_usd=sell_fees_usd, buy_latency_ms=buy_latency_ms, sell_latency_ms=sell_latency_ms,
    )


def _inv_row(outcome="filled", symbol="RVNUSDT", sell_exchange="bybit", started_at=None, filled_qty=2000.0, net_filled_qty=1995.0, avg_fill_price=0.0033, fee_asset=None, fee_amount=0.0):
    return SimpleNamespace(
        outcome=outcome, symbol=symbol, sell_exchange=sell_exchange, started_at=started_at or datetime(2026, 8, 25, 9, 0, 0, tzinfo=UTC),
        filled_qty=filled_qty, net_filled_qty=net_filled_qty, avg_fill_price=avg_fill_price, fee_asset=fee_asset, fee_amount=fee_amount,
    )


# ---- compute_trade_counts ---------------------------------------------------


def test_trade_counts_classifies_every_outcome():
    rows = [
        _arb_row(outcome="both_filled", actual_net_pnl_usd=0.5),
        _arb_row(outcome="both_filled", actual_net_pnl_usd=-0.1),
        _arb_row(outcome="buy_only_neutralized"),
        _arb_row(outcome="neutralization_failed"),
        _arb_row(outcome="no_trade_unprofitable"),
        _arb_row(outcome="no_fill"),
    ]
    counts = compute_trade_counts(rows)
    assert counts.complete_arbitrages == 2
    assert counts.successful == 1  # only the +0.5 one
    assert counts.failed == 2
    assert counts.aborted == 2
    assert counts.neutralizations == 2
    assert counts.unhedged_incidents == 1  # only neutralization_failed


def test_trade_counts_empty():
    counts = compute_trade_counts([])
    assert counts.complete_arbitrages == 0
    assert counts.successful == 0


# ---- compute_real_pnl_breakdown ---------------------------------------------


def test_pnl_breakdown_session_today_total():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    today_start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    session_start = datetime(2026, 8, 25, 11, 0, 0, tzinfo=UTC)
    rows = [
        _arb_row(started_at=datetime(2026, 8, 24, 23, 0, 0, tzinfo=UTC), actual_net_pnl_usd=0.3, predicted_net_profit_usd=0.29),  # yesterday, before today_start and session_start
        _arb_row(started_at=datetime(2026, 8, 25, 5, 0, 0, tzinfo=UTC), actual_net_pnl_usd=0.4, predicted_net_profit_usd=0.41),  # today but before session
        _arb_row(started_at=datetime(2026, 8, 25, 11, 30, 0, tzinfo=UTC), actual_net_pnl_usd=0.2, predicted_net_profit_usd=0.19),  # this session
    ]
    breakdown, last_trades = compute_real_pnl_breakdown(rows, now=now, today_start=today_start, session_start=session_start)
    assert breakdown.session_pnl_usd == pytest.approx(0.2)
    assert breakdown.today_pnl_usd == pytest.approx(0.6)  # 0.4 + 0.2
    assert breakdown.total_pnl_usd == pytest.approx(0.9)  # all three
    assert breakdown.pnl_per_hour_usd == pytest.approx(0.2)  # 0.2 over 1 elapsed hour
    assert len(last_trades) == 3


def test_pnl_breakdown_no_session_start_leaves_session_fields_none():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    today_start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    rows = [_arb_row(started_at=datetime(2026, 8, 25, 5, 0, 0, tzinfo=UTC))]
    breakdown, _ = compute_real_pnl_breakdown(rows, now=now, today_start=today_start, session_start=None)
    assert breakdown.session_pnl_usd is None
    assert breakdown.pnl_per_hour_usd is None


def test_pnl_breakdown_win_rate_and_best_worst():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    today_start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    rows = [
        _arb_row(started_at=today_start, actual_net_pnl_usd=0.5, symbol="ZILUSDT"),
        _arb_row(started_at=today_start, actual_net_pnl_usd=-0.2, symbol="LUNCUSDT"),
        _arb_row(started_at=today_start, actual_net_pnl_usd=0.1, symbol="MANTRAUSDT"),
    ]
    breakdown, _ = compute_real_pnl_breakdown(rows, now=now, today_start=today_start)
    assert breakdown.win_rate_pct == pytest.approx(200 / 3)  # 2/3
    assert breakdown.best_trade.symbol == "ZILUSDT"
    assert breakdown.worst_trade.symbol == "LUNCUSDT"


def test_pnl_breakdown_ignores_non_both_filled_rows():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    today_start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    rows = [_arb_row(outcome="no_fill", started_at=today_start, actual_net_pnl_usd=None)]
    breakdown, last_trades = compute_real_pnl_breakdown(rows, now=now, today_start=today_start)
    assert breakdown.total_pnl_usd == 0.0
    assert last_trades == []


def test_pnl_breakdown_prediction_error():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    today_start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    rows = [
        _arb_row(started_at=today_start, actual_net_pnl_usd=0.20, predicted_net_profit_usd=0.25),
        _arb_row(started_at=today_start, actual_net_pnl_usd=0.30, predicted_net_profit_usd=0.28),
    ]
    breakdown, _ = compute_real_pnl_breakdown(rows, now=now, today_start=today_start)
    assert breakdown.predicted_total_pnl_usd == pytest.approx(0.53)
    assert breakdown.actual_total_pnl_usd == pytest.approx(0.50)
    assert breakdown.prediction_error_usd == pytest.approx(-0.03)
    assert breakdown.max_prediction_error_usd == pytest.approx(0.05)


def test_pnl_breakdown_empty_no_crash():
    now = datetime(2026, 8, 25, 12, 0, 0, tzinfo=UTC)
    today_start = datetime(2026, 8, 25, 0, 0, 0, tzinfo=UTC)
    breakdown, last_trades = compute_real_pnl_breakdown([], now=now, today_start=today_start)
    assert breakdown.total_pnl_usd == 0.0
    assert breakdown.win_rate_pct is None
    assert breakdown.best_trade is None
    assert breakdown.worst_trade is None
    assert last_trades == []


# ---- compute_inventory_constitution_summary ---------------------------------


def test_inventory_constitution_first_is_new_rest_are_recycling():
    rows = [
        _inv_row(symbol="RVNUSDT", sell_exchange="bybit", started_at=datetime(2026, 8, 25, 9, 0, 0, tzinfo=UTC)),
        _inv_row(symbol="RVNUSDT", sell_exchange="bybit", started_at=datetime(2026, 8, 25, 9, 5, 0, tzinfo=UTC)),
        _inv_row(symbol="RVNUSDT", sell_exchange="bybit", started_at=datetime(2026, 8, 25, 9, 10, 0, tzinfo=UTC)),
    ]
    summary = compute_inventory_constitution_summary(rows)
    assert summary.total_constitutions == 3
    assert summary.new_constitutions == 1
    assert summary.recycling_constitutions == 2


def test_inventory_constitution_different_symbols_all_new():
    rows = [
        _inv_row(symbol="RVNUSDT", sell_exchange="bybit"),
        _inv_row(symbol="ZILUSDT", sell_exchange="bybit"),
        _inv_row(symbol="RVNUSDT", sell_exchange="binance"),  # same symbol, different exchange -- still new
    ]
    summary = compute_inventory_constitution_summary(rows)
    assert summary.new_constitutions == 3
    assert summary.recycling_constitutions == 0


def test_inventory_constitution_ignores_unfilled_rows():
    rows = [_inv_row(outcome="no_fill"), _inv_row(outcome="no_trade_edge_insufficient")]
    summary = compute_inventory_constitution_summary(rows)
    assert summary.total_constitutions == 0


def test_inventory_constitution_cost_only_counts_usdt_fees():
    rows = [
        _inv_row(fee_asset="USDT", fee_amount=0.05),
        _inv_row(fee_asset="RVN", fee_amount=2.0, symbol="RVNUSDT", sell_exchange="binance"),  # base-asset fee never touched USDT
    ]
    summary = compute_inventory_constitution_summary(rows)
    assert summary.total_inventory_cost_usd == pytest.approx(0.05)


# ---- compute_inventory_position_status --------------------------------------


def test_inventory_status_ready_low_dust_unknown():
    assert compute_inventory_position_status(10.0, min_notional=5.0) == "READY"
    assert compute_inventory_position_status(2.0, min_notional=5.0) == "LOW"
    assert compute_inventory_position_status(0.0, min_notional=5.0) == "DUST"
    assert compute_inventory_position_status(None) == "UNKNOWN"


# ---- compute_cost_basis_by_asset_exchange -----------------------------------


def test_cost_basis_weighted_average_across_buy_and_inventory_fills():
    arb_rows = [_arb_row(symbol="RVNUSDT", buy_exchange="binance", buy_net_filled_qty=100.0, buy_avg_fill_price=0.003)]
    inv_rows = [_inv_row(symbol="RVNUSDT", sell_exchange="binance", net_filled_qty=100.0, avg_fill_price=0.005)]
    basis = compute_cost_basis_by_asset_exchange(arb_rows, inv_rows)
    # 100 units @ 0.003 (arbitrage buy) + 100 units @ 0.005 (inventory constitution) -> weighted avg 0.004
    assert basis[("RVN", "binance")] == pytest.approx(0.004)


def test_cost_basis_ignores_non_filled_rows():
    arb_rows = [_arb_row(outcome="no_fill")]
    inv_rows = [_inv_row(outcome="no_fill")]
    assert compute_cost_basis_by_asset_exchange(arb_rows, inv_rows) == {}


def test_missed_opportunity_causes_buckets_by_keyword():
    arb_rows = [
        _arb_row(outcome="no_trade_unprofitable"),
        _arb_row(outcome="no_trade_unprofitable"),
    ]
    arb_rows[0].reason = "buy notional 0.27 below min_notional 5.0"
    arb_rows[1].reason = "not net-positive at the real common quantity"
    inv_rows = [_inv_row(outcome="no_trade_edge_insufficient")]
    inv_rows[0].reason = "no common quantity after step rounding"
    causes = compute_missed_opportunity_causes(arb_rows, inv_rows)
    by_cause = {c.cause: c.count for c in causes}
    assert by_cause["MIN_NOTIONAL"] == 1
    assert by_cause["EDGE_DISAPPEARED"] == 1
    assert by_cause["INVENTORY"] == 1


def test_missed_opportunity_causes_ignores_filled_rows():
    arb_rows = [_arb_row(outcome="both_filled")]
    arb_rows[0].reason = None
    assert compute_missed_opportunity_causes(arb_rows, []) == []


def test_missed_opportunity_causes_unrecognized_text_is_other():
    arb_rows = [_arb_row(outcome="no_fill")]
    arb_rows[0].reason = "something totally unrelated happened"
    causes = compute_missed_opportunity_causes(arb_rows, [])
    assert causes[0].cause == "OTHER"


def test_cost_basis_keyed_separately_per_exchange():
    arb_rows = [
        _arb_row(symbol="RVNUSDT", buy_exchange="binance", buy_net_filled_qty=100.0, buy_avg_fill_price=0.003),
        _arb_row(symbol="RVNUSDT", buy_exchange="bybit", buy_net_filled_qty=100.0, buy_avg_fill_price=0.006),
    ]
    basis = compute_cost_basis_by_asset_exchange(arb_rows, [])
    assert basis[("RVN", "binance")] == pytest.approx(0.003)
    assert basis[("RVN", "bybit")] == pytest.approx(0.006)
