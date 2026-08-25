import uuid
from dataclasses import replace

from app.execution.dual_leg_quote import DualLegQuote
from app.execution.rejection_classifier import classify_rejection, diagnose_cost_basis_gap
from app.execution.true_economic_ledger import CostBasisPool
from app.execution.true_economic_pretrade import ExecutabilityCheck, TrueEconomicQuote


def _quote(**overrides) -> DualLegQuote:
    base = dict(
        opportunity_id=uuid.uuid4(), symbol="RVN/USDT", buy_exchange="okx", sell_exchange="binance",
        buy_execution_price=0.0032, sell_execution_price=0.0033, executable_qty=3000.0,
        buy_valid_qty=3000.0, sell_valid_qty=3000.0, gross_spread_pct=3.0, buy_fee_usd=0.01, sell_fee_usd=0.01,
        buy_slippage_pct=0.1, sell_slippage_pct=0.1, buy_quote_age_ms=50.0, sell_quote_age_ms=50.0,
        dual_leg_latency_ms=10.0, net_profit_usd=0.05, net_return_bps=50.0,
        buy_min_notional_pass=True, buy_lot_size_pass=True, sell_min_notional_pass=True, sell_lot_size_pass=True,
        buy_tradable=True, sell_tradable=True, executable=True, reason=None,
        buy_fee_source="real_account_fee", sell_fee_source="real_account_fee", computed_at=0.0,
    )
    base.update(overrides)
    return DualLegQuote(**base)


def _te_quote(**overrides) -> TrueEconomicQuote:
    base = dict(
        sell_inventory_cost_basis_usd=9.9, expected_net_sell_proceeds_usd=10.0, sell_side_realized_pnl_usd=0.1,
        new_buy_cost_usd=9.9, new_buy_mark_to_market_value_usd=10.0, expected_buy_inventory_delta_usd=0.1,
        expected_rebalancing_cost_usd=0.0, expected_total_fees_usd=0.02, expected_true_wealth_delta_usd=0.18,
        would_trade=True, reason="expected_true_wealth_delta_usd > required_safety_margin_usd",
        new_sell_pool=None, new_buy_pool=None,
    )
    base.update(overrides)
    return TrueEconomicQuote(**base)


def _executability(**overrides) -> ExecutabilityCheck:
    base = dict(
        capital_required_usd=10.0, capital_available_usd=12.0, inventory_required_qty=3000.0,
        inventory_available_qty=3000.0, capital_sufficient=True, inventory_sufficient=True,
        true_economic_positive=True, executable_now=True, blocker=None,
    )
    base.update(overrides)
    return ExecutabilityCheck(**base)


def test_not_tradable_wins_over_everything_else():
    result = classify_rejection(
        quote=_quote(buy_tradable=False), te_quote=_te_quote(), executability=_executability(),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "NOT_TRADABLE"


def test_below_min_notional_from_buy_leg():
    result = classify_rejection(
        quote=_quote(buy_min_notional_pass=False), te_quote=_te_quote(), executability=_executability(),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "BELOW_MIN_NOTIONAL"


def test_below_min_notional_from_sell_lot_size():
    result = classify_rejection(
        quote=_quote(sell_lot_size_pass=False), te_quote=_te_quote(), executability=_executability(),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "BELOW_MIN_NOTIONAL"


def test_insufficient_depth_from_high_slippage():
    result = classify_rejection(
        quote=_quote(buy_slippage_pct=100.0), te_quote=_te_quote(), executability=_executability(),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "INSUFFICIENT_DEPTH"


def test_unknown_sell_cost_basis_when_te_quote_has_no_basis():
    result = classify_rejection(
        quote=_quote(), te_quote=_te_quote(sell_inventory_cost_basis_usd=None, expected_true_wealth_delta_usd=None, would_trade=False),
        executability=_executability(true_economic_positive=False, executable_now=False),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "UNKNOWN_SELL_COST_BASIS"


def test_reserve_floor_distinguished_from_insufficient_capital():
    # real balance (20.5) covers the 10.0 required, but capital_available (post-floor) does not
    result = classify_rejection(
        quote=_quote(), te_quote=_te_quote(),
        executability=_executability(capital_available_usd=0.0, capital_sufficient=False, executable_now=False),
        real_balance_before_floor_usd=20.5, reserve_floor_usd=25.0,
    )
    assert result == "RESERVE_FLOOR"


def test_genuinely_insufficient_capital_when_even_raw_balance_too_low():
    result = classify_rejection(
        quote=_quote(), te_quote=_te_quote(),
        executability=_executability(capital_available_usd=2.0, capital_sufficient=False, executable_now=False),
        real_balance_before_floor_usd=2.0, reserve_floor_usd=25.0,
    )
    assert result == "INSUFFICIENT_CAPITAL"


def test_insufficient_sell_inventory():
    result = classify_rejection(
        quote=_quote(), te_quote=_te_quote(),
        executability=_executability(inventory_sufficient=False, inventory_available_qty=100.0, executable_now=False),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "INSUFFICIENT_SELL_INVENTORY"


def test_not_true_economic_positive_when_everything_else_clears():
    result = classify_rejection(
        quote=_quote(), te_quote=_te_quote(expected_true_wealth_delta_usd=-0.05, would_trade=False),
        executability=_executability(true_economic_positive=False, executable_now=False),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "NOT_TRUE_ECONOMIC_POSITIVE"


def test_other_when_fully_executable():
    result = classify_rejection(
        quote=_quote(), te_quote=_te_quote(), executability=_executability(),
        real_balance_before_floor_usd=100.0, reserve_floor_usd=25.0,
    )
    assert result == "OTHER"


# --- cost basis gap diagnosis ---

def test_zero_real_balance_is_not_a_tracking_gap():
    pool = CostBasisPool(exchange="okx", asset="RVN", qty=0.0, cost_usd=0.0)
    d = diagnose_cost_basis_gap(pool, real_balance_qty=0.0)
    assert d.category == "ZERO_REAL_BALANCE"


def test_ledger_matches_real_balance():
    pool = CostBasisPool(exchange="binance", asset="RVN", qty=1000.0, cost_usd=3.2)
    d = diagnose_cost_basis_gap(pool, real_balance_qty=1000.0000001)
    assert d.category == "LEDGER_MATCHES_REAL_BALANCE"


def test_ledger_understates_real_balance_pre_session_inventory():
    pool = CostBasisPool(exchange="binance", asset="RVN", qty=0.0, cost_usd=0.0)
    d = diagnose_cost_basis_gap(pool, real_balance_qty=6407.427)
    assert d.category == "LEDGER_UNDERSTATES_REAL_BALANCE"
    assert "acquired outside this session" in d.detail


def test_ledger_overstates_real_balance_is_flagged_not_hidden():
    pool = CostBasisPool(exchange="bybit", asset="SAND", qty=500.0, cost_usd=20.0)
    d = diagnose_cost_basis_gap(pool, real_balance_qty=340.843)
    assert d.category == "LEDGER_OVERSTATES_REAL_BALANCE"
    assert "cannot cause an oversell" in d.detail


def test_diagnosis_never_uses_current_price_only_quantities():
    pool = CostBasisPool(exchange="binance", asset="ZIL", qty=100.0, cost_usd=5.0)
    d = diagnose_cost_basis_gap(pool, real_balance_qty=100.0)
    assert d.ledger_qty == 100.0
    assert d.real_balance_qty == 100.0
