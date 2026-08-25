from app.execution.binance_live_trade_client import BinanceOrderResult
from app.execution.bybit_live_trade_client import BybitOrderStatus
from app.operations.order_intent_log import OrderIntent
from app.operations.persistent_kill_switch import KillSwitchState
from app.operations.preflight import evaluate_preflight

CLEAN_KILL_SWITCH = KillSwitchState(engaged=False)
ENGAGED_KILL_SWITCH = KillSwitchState(engaged=True, reason="NEUTRALIZATION FAILED", engaged_at="t0", incident={})


def _open_order(symbol="RVNUSDT") -> BinanceOrderResult:
    return BinanceOrderResult(symbol=symbol, order_id=1, client_order_id="x", status="NEW", executed_qty=0.0, cumulative_quote_qty=0.0, fills=[], raw={})


def _bybit_open_order(symbol="RVNUSDT") -> BybitOrderStatus:
    return BybitOrderStatus(order_id="1", order_link_id="x", symbol=symbol, side="Buy", order_status="New", cum_exec_qty=0.0, cum_exec_value=0.0, cum_exec_fee=0.0, avg_price=None, raw={})


def _all_clear(**overrides):
    defaults = dict(
        binance_open_orders=[], bybit_open_orders=[], unresolved_intents=[],
        balances_reachable=True, ledger_reachable=True, kill_switch_state=CLEAN_KILL_SWITCH,
    )
    defaults.update(overrides)
    return evaluate_preflight(**defaults)


def test_all_clear_is_safe_to_resume():
    result = _all_clear()
    assert result.safe_to_resume is True
    assert set(result.checks) == {"OPEN_ORDERS", "UNKNOWN_ORDERS", "BALANCES", "LEDGER_STATE", "KILL_SWITCH"}
    assert all(v.startswith("PASS") for v in result.checks.values())


def test_open_order_on_binance_blocks_resume_first():
    result = _all_clear(binance_open_orders=[_open_order()])
    assert result.safe_to_resume is False
    assert "OPEN ORDERS" in result.reason
    assert result.checks["OPEN_ORDERS"].startswith("FAIL")
    assert "UNKNOWN_ORDERS" not in result.checks  # short-circuited -- never masks by checking further


def test_open_order_on_bybit_also_blocks():
    result = _all_clear(bybit_open_orders=[_bybit_open_order()])
    assert result.safe_to_resume is False
    assert "OPEN ORDERS" in result.reason


def test_unresolved_intent_blocks_resume():
    intent = OrderIntent(intent_id="i1", purpose="ARBITRAGE", exchange="binance", symbol="RVN/USDT", notional_usdt=10.0,
                          client_order_id=None, started_at="t0", resolved=False, resolved_at=None, resolved_outcome=None)
    result = _all_clear(unresolved_intents=[intent])
    assert result.safe_to_resume is False
    assert "UNRESOLVED ORDER INTENT" in result.reason
    assert "ARBITRAGE" in result.checks["UNKNOWN_ORDERS"]
    assert "BALANCES" not in result.checks  # short-circuited


def test_unreachable_balances_blocks_resume():
    result = _all_clear(balances_reachable=False)
    assert result.safe_to_resume is False
    assert "BALANCES" in result.reason
    assert "LEDGER_STATE" not in result.checks


def test_unreachable_ledger_blocks_resume():
    result = _all_clear(ledger_reachable=False)
    assert result.safe_to_resume is False
    assert "LEDGER" in result.reason
    assert "KILL_SWITCH" not in result.checks


def test_engaged_kill_switch_blocks_resume_even_if_everything_else_is_clean():
    """The final and most important gate: a persisted kill switch from
    a PRIOR run must block resume even when open orders, intents,
    balances, and the ledger are all completely healthy."""
    result = _all_clear(kill_switch_state=ENGAGED_KILL_SWITCH)
    assert result.safe_to_resume is False
    assert "KILL SWITCH" in result.reason
    assert "NEUTRALIZATION FAILED" in result.reason


def test_checks_are_recorded_in_order_up_to_the_failure():
    result = _all_clear(kill_switch_state=ENGAGED_KILL_SWITCH)
    assert list(result.checks.keys()) == ["OPEN_ORDERS", "UNKNOWN_ORDERS", "BALANCES", "LEDGER_STATE", "KILL_SWITCH"]


def test_okx_open_order_blocks_resume():
    """okx_open_orders defaults to () for backward compatibility, but
    when supplied it must be checked exactly like binance/bybit's --
    one real open order on OKX at startup is just as anomalous as one
    on either of the other two exchanges."""
    result = _all_clear(okx_open_orders=[{"ordId": "1"}])
    assert result.safe_to_resume is False
    assert "OPEN ORDERS" in result.reason
    assert "1 okx" in result.checks["OPEN_ORDERS"]


def test_okx_open_orders_defaults_to_empty_and_stays_backward_compatible():
    """Every pre-existing (Binance/Bybit-only) caller that never passes
    okx_open_orders at all must be completely unaffected."""
    result = _all_clear()
    assert result.safe_to_resume is True
    assert result.checks["OPEN_ORDERS"] == "PASS -- none found on any exchange"
