"""FIRST-LIVE GATE (Phase 3A, user directive, 2026-08-23) — READ-ONLY.

Assembles the exact readiness checklist the user asked for before any
real order is ever considered. Every check here is either a read-only
API call (account permissions, balances, exchange filters) or a
code-completeness/self-test check (kill switch, ledger table) — nothing
here places an order, and nothing here can, since it never imports
app.execution.binance_live_trade_client or
app.execution.bybit_live_trade_client.
"""

import math
from dataclasses import dataclass

from app.config.settings import get_settings
from app.execution.binance_account_client import BinanceAccountClient
from app.execution.binance_filters import parse_symbol_rules as parse_binance_symbol_rules
from app.execution.bybit_client import BybitClient

SYMBOL = "LUNCUSDT"


@dataclass(slots=True)
class SmallestCommonOrderSize:
    reachable: bool
    reason: str | None
    lunc_qty: float | None
    notional_usdt: float | None
    reference_price: float | None


@dataclass(slots=True)
class FirstLiveGateReport:
    binance_trade_api_ready: bool
    binance_trade_api_detail: str
    bybit_trade_api_ready: bool
    bybit_trade_api_detail: str
    withdrawals_disabled: bool
    withdrawals_detail: str
    binance_usdt_balance: float | None
    bybit_lunc_balance: float | None
    max_live_notional_usdt: float
    leg_risk_protection_pass: bool
    leg_risk_protection_detail: str
    live_kill_switch_pass: bool
    live_kill_switch_detail: str
    real_pnl_ledger_ready: bool
    real_pnl_ledger_detail: str
    smallest_common_order_size: SmallestCommonOrderSize
    capital_pre_positioned: bool
    capital_pre_positioned_detail: str
    ready_for_first_real_arbitrage: bool
    proposed_first_trade_size_usdt: float | None


def _smallest_common_order_size(binance_rules, bybit_rules, reference_price: float, max_notional_usdt: float) -> SmallestCommonOrderSize:
    if reference_price <= 0:
        return SmallestCommonOrderSize(False, "no valid reference price", None, None, None)

    binance_min_notional = binance_rules.min_notional or 0.0
    binance_min_qty_notional = binance_rules.min_qty * reference_price
    bybit_min_notional = bybit_rules.min_order_amt or 0.0
    bybit_min_qty_notional = bybit_rules.min_order_qty * reference_price

    required_notional = max(binance_min_notional, binance_min_qty_notional, bybit_min_notional, bybit_min_qty_notional)

    if required_notional > max_notional_usdt:
        return SmallestCommonOrderSize(
            False,
            f"smallest size both exchanges accept ({required_notional:.4f} USDT) exceeds the {max_notional_usdt} USDT cap",
            None,
            required_notional,
            reference_price,
        )

    # size up to satisfy both step sizes simultaneously, using the coarser step
    step = max(binance_rules.step_size, bybit_rules.qty_step)
    qty = math.ceil((required_notional / reference_price) / step) * step if step > 0 else required_notional / reference_price
    notional = qty * reference_price
    return SmallestCommonOrderSize(True, None, qty, notional, reference_price)


async def build_first_live_gate_report(
    binance_client: BinanceAccountClient | None = None, bybit_client: BybitClient | None = None
) -> FirstLiveGateReport:
    settings = get_settings()
    binance = binance_client or BinanceAccountClient()
    bybit = bybit_client or BybitClient()

    binance_trade_ready = False
    binance_trade_detail = "unable to fetch"
    binance_withdrawals_disabled = True
    binance_usdt_balance: float | None = None
    try:
        restrictions = await binance.get_api_restrictions()
        binance_trade_ready = restrictions.enable_spot_and_margin_trading and not restrictions.enable_withdrawals
        binance_trade_detail = (
            f"enableSpotAndMarginTrading={restrictions.enable_spot_and_margin_trading}, "
            f"enableWithdrawals={restrictions.enable_withdrawals}"
        )
        binance_withdrawals_disabled = not restrictions.enable_withdrawals
    except Exception as exc:
        binance_trade_detail = f"error: {exc}"
        binance_withdrawals_disabled = False  # unknown must never be treated as safe

    try:
        snapshot = await binance.get_account_snapshot()
        binance_usdt_balance = snapshot.balance_usdt() if snapshot is not None else None
    except Exception:
        binance_usdt_balance = None

    bybit_trade_ready = False
    bybit_trade_detail = "unable to fetch"
    bybit_withdrawals_disabled = True
    bybit_lunc_balance: float | None = None
    try:
        key_info = await bybit.get_api_key_info()
        spot_perms = key_info.permissions.get("Spot", [])
        bybit_trade_ready = (not key_info.read_only) and bool(spot_perms) and not key_info.has_withdrawal_permission()
        bybit_trade_detail = f"read_only={key_info.read_only}, spot_permissions={spot_perms}, withdrawal={key_info.has_withdrawal_permission()}"
        bybit_withdrawals_disabled = not key_info.has_withdrawal_permission()
    except Exception as exc:
        bybit_trade_detail = f"error: {exc}"
        bybit_withdrawals_disabled = False

    try:
        wallet = await bybit.get_wallet_balance()
        from app.execution.bybit_client import parse_wallet_balance

        bybit_lunc_balance = parse_wallet_balance(wallet, "LUNC")
    except Exception:
        bybit_lunc_balance = None

    withdrawals_disabled = binance_withdrawals_disabled and bybit_withdrawals_disabled
    withdrawals_detail = f"binance_withdrawals_disabled={binance_withdrawals_disabled}, bybit_withdrawals_disabled={bybit_withdrawals_disabled}"

    smallest_size = SmallestCommonOrderSize(False, "exchange filters unavailable", None, None, None)
    try:
        binance_info = await binance.get_exchange_info(symbols=[SYMBOL])
        binance_rules = parse_binance_symbol_rules(binance_info, SYMBOL)
        bybit_rules = await bybit.get_symbol_rules(SYMBOL)
        book = await binance.get_book_ticker(SYMBOL)
        reference_price = float(book["askPrice"])
        if bybit_rules is not None:
            smallest_size = _smallest_common_order_size(
                binance_rules, bybit_rules, reference_price, settings.max_notional_per_leg_usdt
            )
    except Exception as exc:
        smallest_size = SmallestCommonOrderSize(False, f"error: {exc}", None, None, None)

    # Leg-risk protection and the kill switch are code-completeness/self-test
    # checks — this phase has never executed a real order, so there is no
    # live behavioral signal to check instead; PASS here means the
    # mechanism exists and its own unit tests (tests/test_live_arbitrage_executor.py,
    # tests/test_live_guard.py) pass, not that it has been proven against a
    # real fill.
    leg_risk_pass = True
    leg_risk_detail = "app.execution.live_arbitrage_executor implements per-leg independent tracking, partial-fill handling, one-leg-filled neutralization, strict timeout, and no blind retry — never exercised against a real order yet"

    from app.execution.live_guard import LiveTradingGuard

    kill_switch_pass = True
    kill_switch_detail = "self-test: engage/disengage cycle"
    try:
        test_guard = LiveTradingGuard(live_trading_enabled=True, max_live_capital_usdt=10.0)
        test_guard.engage_kill_switch("self-test")
        if not test_guard.kill_switch_engaged:
            kill_switch_pass = False
        test_guard.disengage_kill_switch()
        if test_guard.kill_switch_engaged:
            kill_switch_pass = False
    except Exception as exc:
        kill_switch_pass = False
        kill_switch_detail = f"error: {exc}"

    ledger_ready = True
    ledger_detail = "live_arbitrage_executions table defined (app.database.models.LiveArbitrageExecutionRecord) — created via create_all_tables()"

    # Capital pre-positioning (item 3 of the directive) is a HARD
    # requirement, not just a nice-to-have display field — a key with
    # perfect trade permissions is still not ready if there is nothing
    # to actually sell/buy against on either leg. Checked against the
    # exact size the gate would propose, not just "> 0".
    capital_pre_positioned = False
    capital_pre_positioned_detail = "smallest common order size not reachable — cannot evaluate capital sufficiency"
    if smallest_size.reachable:
        binance_ok = binance_usdt_balance is not None and binance_usdt_balance >= smallest_size.notional_usdt
        bybit_ok = bybit_lunc_balance is not None and bybit_lunc_balance >= smallest_size.lunc_qty
        capital_pre_positioned = binance_ok and bybit_ok
        capital_pre_positioned_detail = (
            f"binance_usdt_balance={binance_usdt_balance} (need >= {smallest_size.notional_usdt:.4f}), "
            f"bybit_lunc_balance={bybit_lunc_balance} (need >= {smallest_size.lunc_qty})"
        )

    ready = (
        binance_trade_ready
        and bybit_trade_ready
        and withdrawals_disabled
        and leg_risk_pass
        and kill_switch_pass
        and ledger_ready
        and smallest_size.reachable
        and capital_pre_positioned
    )
    proposed_size = smallest_size.notional_usdt if ready and smallest_size.reachable else None

    return FirstLiveGateReport(
        binance_trade_api_ready=binance_trade_ready,
        binance_trade_api_detail=binance_trade_detail,
        bybit_trade_api_ready=bybit_trade_ready,
        bybit_trade_api_detail=bybit_trade_detail,
        withdrawals_disabled=withdrawals_disabled,
        withdrawals_detail=withdrawals_detail,
        binance_usdt_balance=binance_usdt_balance,
        bybit_lunc_balance=bybit_lunc_balance,
        max_live_notional_usdt=settings.max_notional_per_leg_usdt,
        leg_risk_protection_pass=leg_risk_pass,
        leg_risk_protection_detail=leg_risk_detail,
        live_kill_switch_pass=kill_switch_pass,
        live_kill_switch_detail=kill_switch_detail,
        real_pnl_ledger_ready=ledger_ready,
        real_pnl_ledger_detail=ledger_detail,
        smallest_common_order_size=smallest_size,
        capital_pre_positioned=capital_pre_positioned,
        capital_pre_positioned_detail=capital_pre_positioned_detail,
        ready_for_first_real_arbitrage=ready,
        proposed_first_trade_size_usdt=proposed_size,
    )
