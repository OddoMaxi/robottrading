"""INVENTORY CONSTITUTION EXECUTOR (user directive, 2026-08-24) — Binance
<-> Bybit SPOT ONLY. Automatically buys the MINIMUM base-asset quantity
needed on the SELL exchange to unblock a real, profitable, persistent
cross-exchange opportunity currently classified INVENTORY_MISSING.

DANGER: this module can place ONE real market order — a SPOT BUY on the
SELL exchange, to constitute inventory there. Never a sell, never on the
buy exchange, never leveraged/margin/futures (no such client exists
anywhere in this codebase to even call). It reuses the EXACT same
leg-risk discipline as app.execution.live_arbitrage_executor: a unique
client order id per attempt, an ACK is NEVER treated as a fill
confirmation, every submission is polled to a genuinely terminal status
or a strict timeout (a timeout is UNKNOWN, never assumed either way, and
engages the inventory kill switch), and this module is NEVER imported by
main.py's automatic loop — every call is a deliberate, individually
authorized act (tests/test_phase3a_isolation.py proves both this module
and live_arbitrage_executor.py are the ONLY two files anywhere under
app/ allowed to import a live-trade client).

Deliberately does NOT share code with live_arbitrage_executor.py (a
conscious choice, not an oversight) — that module is already tested and
in place; duplicating its small, reviewable order-placement/polling
primitives here for the single-leg case avoids any risk of a refactor
regressing the already-validated dual-leg executor right as real orders
begin.

This module does NOT execute the arbitrage itself. Constituting
inventory and executing the arbitrage that inventory unblocks are two
separately authorized actions (user directive, 2026-08-24: "pour le
premier test uniquement, ne lance pas encore automatiquement
l'arbitrage après l'achat d'inventaire"). Whether to proceed to the
arbitrage after a successful fill is the CALLER's decision, informed by
InventoryConstitutionResult.ready_for_arbitrage — this module itself
never calls execute_one_arbitrage and never imports it.
"""

import asyncio
import logging
import math
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app.execution.binance_account_client import BinanceAccountClient
from app.execution.binance_filters import parse_symbol_rules as parse_binance_symbol_rules
from app.execution.binance_live_trade_client import BinanceLiveTradeClient
from app.execution.bybit_client import BybitClient
from app.execution.bybit_live_trade_client import BybitLiveTradeClient
from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_dual_leg_quote
from app.execution.inventory_guard import InventoryExecutionRefused, inventory_guard
from app.execution.reality_quote import DEFAULT_TAKER_FEE_RATE

logger = logging.getLogger(__name__)

LEG_CONFIRMATION_TIMEOUT_SECONDS = 15.0
LEG_POLL_INTERVAL_SECONDS = 0.5
EXCHANGES = ("binance", "bybit")

# Stated, round assumption (never fitted to any observed data) — covers
# the sell-exchange taker fee (paid in the base asset on a spot buy) and
# price drift between constituting inventory and eventually selling it,
# so a bare-minimum purchase doesn't fall short of what a later sell
# actually needs. Same figure used in the real-size audits this session.
TECHNICAL_MARGIN_PCT = 0.05


class InventoryConstitutionOutcome(StrEnum):
    NO_TRADE_REFUSED = "no_trade_refused"  # inventory_guard refused before any order was considered
    NO_TRADE_EDGE_INSUFFICIENT = "no_trade_edge_insufficient"  # fresh re-check found the edge no longer positive — no order sent
    NO_FILL = "no_fill"  # order rejected/expired with zero fill
    FILLED = "filled"
    UNKNOWN = "unknown"  # timeout/ambiguous response — kill switch engaged, manual reconciliation required


@dataclass(slots=True)
class InventoryConstitutionResult:
    attempt_id: uuid.UUID
    symbol: str
    buy_exchange_for_arbitrage: str
    sell_exchange: str
    outcome: InventoryConstitutionOutcome
    reason: str | None

    pre_purchase_net_edge_usd: float | None = None
    required_base_qty: float | None = None
    requested_notional_usdt: float | None = None

    order_client_id: str | None = None
    order_exchange_id: str | None = None
    order_status: str | None = None
    filled_qty: float = 0.0
    avg_fill_price: float | None = None
    fee_usd: float | None = None
    submitted_at: float | None = None
    confirmed_at: float | None = None

    post_fill_net_edge_usd: float | None = None
    edge_still_valid_after_fill: bool | None = None
    ready_for_arbitrage: bool = False

    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None


@dataclass(slots=True)
class _NormalizedFillStatus:
    order_id: str
    is_terminal: bool
    is_filled: bool
    filled_qty: float
    avg_fill_price: float | None
    fee_usd: float
    raw_status: str


def compute_required_inventory_qty(bare_qty: float, taker_fee_rate: float, step_size: float, technical_margin_pct: float = TECHNICAL_MARGIN_PCT) -> float:
    """Pure function. bare_qty is compute_dual_leg_quote's own
    executable_qty (already step-rounded on both legs) — the exact
    quantity the arbitrage's sell leg would need. Inflated for the
    sell-exchange taker fee (paid in the base asset on a spot buy —
    constituting inventory IS itself a spot buy on the sell exchange)
    and the stated technical margin, then rounded UP to the exchange's
    real step size so the held quantity always covers at least bare_qty
    even after fee deduction and rounding."""
    if bare_qty <= 0:
        return 0.0
    required = bare_qty / max(1e-9, (1 - taker_fee_rate)) * (1 + technical_margin_pct)
    if step_size <= 0:
        return required
    return math.ceil(required / step_size) * step_size


class InventoryConstitutionExecutor:
    def __init__(
        self,
        binance_read: BinanceAccountClient | None = None,
        binance_trade: BinanceLiveTradeClient | None = None,
        bybit_read: BybitClient | None = None,
        bybit_trade: BybitLiveTradeClient | None = None,
    ) -> None:
        self._binance_read = binance_read or BinanceAccountClient()
        self._binance_trade = binance_trade or BinanceLiveTradeClient()
        self._bybit_read = bybit_read or BybitClient()
        self._bybit_trade = bybit_trade or BybitLiveTradeClient()

    async def _fetch_leg_snapshot(self, symbol: str, exchange: str, side: str) -> LegSnapshot | None:
        """Identical shape to live_arbitrage_executor's own leg fetch —
        duplicated deliberately, see module docstring."""
        now = time.time()
        if exchange == "binance":
            book = await self._binance_read.get_book_ticker(symbol)
            depth = await self._binance_read.get_order_book_depth(symbol, limit=20)
            info = await self._binance_read.get_exchange_info(symbols=[symbol])
            rules = parse_binance_symbol_rules(info, symbol)
            fee = await self._binance_read.get_trade_fee(symbol)
            depth_levels = [(float(p), float(q)) for p, q in depth.get("asks" if side == "buy" else "bids", [])]
            return LegSnapshot(
                exchange="binance", side=side, best_bid=float(book["bidPrice"]), best_ask=float(book["askPrice"]),
                depth_levels=depth_levels, min_qty=rules.min_qty, step_size=rules.step_size, tick_size=rules.tick_size,
                min_notional=rules.min_notional, tradable=(rules.status == "TRADING" and rules.is_spot_trading_allowed),
                maker_fee_rate=fee.maker_fee_rate if fee is not None else None,
                taker_fee_rate=fee.taker_fee_rate if fee is not None else DEFAULT_TAKER_FEE_RATE,
                fee_source="real_account_fee" if fee is not None else "estimated_default",
                fetch_started_at=now, fetch_completed_at=time.time(),
            )
        else:
            book = await self._bybit_read.get_book_ticker(symbol)
            if book is None:
                return None
            depth = await self._bybit_read.get_order_book_depth(symbol, limit=50)
            rules = await self._bybit_read.get_symbol_rules(symbol)
            if rules is None:
                return None
            fee = await self._bybit_read.get_fee_rate(symbol)
            side_key = "a" if side == "buy" else "b"
            depth_levels = [(float(p), float(q)) for p, q in depth.get("result", {}).get(side_key, [])]
            return LegSnapshot(
                exchange="bybit", side=side, best_bid=book.bid_price, best_ask=book.ask_price,
                depth_levels=depth_levels, min_qty=rules.min_order_qty, step_size=rules.qty_step, tick_size=rules.tick_size,
                min_notional=rules.min_order_amt, tradable=rules.is_tradable,
                maker_fee_rate=fee.maker_fee_rate if fee is not None else None,
                taker_fee_rate=fee.taker_fee_rate if fee is not None else DEFAULT_TAKER_FEE_RATE,
                fee_source="real_account_fee" if fee is not None else "estimated_default",
                fetch_started_at=now, fetch_completed_at=time.time(),
            )

    async def _fresh_arbitrage_quote(self, symbol: str, buy_exchange: str, sell_exchange: str, notional_usdt: float) -> DualLegQuote | None:
        try:
            buy_leg = await self._fetch_leg_snapshot(symbol, buy_exchange, "buy")
            sell_leg = await self._fetch_leg_snapshot(symbol, sell_exchange, "sell")
            if buy_leg is None or sell_leg is None:
                return None
            return compute_dual_leg_quote(
                opportunity_id=uuid.uuid4(), symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg,
                master_requested_size_usd=notional_usdt, micro_live_cap_usdt=notional_usdt,
            )
        except Exception as exc:
            logger.warning("inventory-constitution: fresh quote failed for %s %s->%s: %s", symbol, buy_exchange, sell_exchange, exc)
            return None

    async def _place_market_buy(self, exchange: str, symbol: str, notional_usdt: float, client_order_id: str) -> None:
        """Identical shape to live_arbitrage_executor's own — SPOT
        market buy, sized by quote notional, never a leveraged/margin
        order type (no such call exists to make).

        Bybit fix (2026-08-24): place_market_order now sends
        marketUnit="quoteCoin" for every Buy, meaning qty IS the USDT
        notional itself — passed straight through, never pre-converted
        to an estimated base-asset quantity via a book price (that
        conversion is exactly what made the caller's qty mean something
        different from what the wire payload now says it means)."""
        if exchange == "binance":
            await self._binance_trade.place_market_order(symbol, "BUY", client_order_id=client_order_id, quote_order_qty=notional_usdt)
        else:
            await self._bybit_trade.place_market_order(symbol, "Buy", qty=notional_usdt, order_link_id=client_order_id)

    async def _get_status(self, exchange: str, symbol: str, client_order_id: str) -> _NormalizedFillStatus | None:
        if exchange == "binance":
            result = await self._binance_trade.get_order_status(symbol, orig_client_order_id=client_order_id)
            fees_by_asset = result.total_fees_by_asset()
            non_usdt_fees = {a: v for a, v in fees_by_asset.items() if a != "USDT"}
            if non_usdt_fees:
                logger.warning("inventory-constitution: order fee(s) charged in non-USDT asset(s), excluded from fee_usd: %s", non_usdt_fees)
            return _NormalizedFillStatus(
                order_id=str(result.order_id), is_terminal=result.is_terminal, is_filled=result.is_filled,
                filled_qty=result.executed_qty, avg_fill_price=result.average_fill_price(),
                fee_usd=fees_by_asset.get("USDT", 0.0), raw_status=result.status,
            )
        else:
            status = await self._bybit_trade.get_order_status(symbol, order_link_id=client_order_id)
            if status is None:
                return None
            return _NormalizedFillStatus(
                order_id=status.order_id, is_terminal=status.is_terminal, is_filled=status.is_filled,
                filled_qty=status.cum_exec_qty, avg_fill_price=status.avg_price,
                fee_usd=status.cum_exec_fee, raw_status=status.order_status,
            )

    async def _await_terminal(self, poll_fn, timeout_seconds: float) -> tuple[bool, _NormalizedFillStatus | None]:
        deadline = time.time() + timeout_seconds
        last_result = None
        while time.time() < deadline:
            try:
                last_result = await poll_fn()
            except Exception as exc:
                logger.warning("inventory-constitution: status poll failed, retrying: %s", exc)
                last_result = None
            if last_result is not None and last_result.is_terminal:
                return True, last_result
            await asyncio.sleep(LEG_POLL_INTERVAL_SECONDS)
        return False, last_result

    async def constitute_inventory(
        self,
        symbol: str,
        buy_exchange_for_arbitrage: str,
        sell_exchange: str,
        requested_notional_usdt: float,
    ) -> InventoryConstitutionResult:
        """The leg-risk-managed order placement itself (steps 4-9 of the
        user's own numbered process) — assumes the CALLER has already
        run steps 1-3 (fresh edge check, required-qty calculation,
        reuse/persistence justification via
        app.execution.inventory_manager.score_direction_for_inventory)
        and decided this symbol is worth attempting. This method still
        re-verifies the edge itself with brand-new data immediately
        before submitting (never trusts a quote computed even seconds
        ago for the actual go/no-go decision — same discipline as
        live_arbitrage_executor) and independently re-checks the guard —
        never assumes an external caller remembered to."""
        if buy_exchange_for_arbitrage not in EXCHANGES or sell_exchange not in EXCHANGES or buy_exchange_for_arbitrage == sell_exchange:
            raise ValueError(f"buy/sell exchange must be distinct values from {EXCHANGES}, got {buy_exchange_for_arbitrage!r}/{sell_exchange!r}")

        attempt_id = uuid.uuid4()
        result = InventoryConstitutionResult(
            attempt_id=attempt_id, symbol=symbol, buy_exchange_for_arbitrage=buy_exchange_for_arbitrage,
            sell_exchange=sell_exchange, outcome=InventoryConstitutionOutcome.NO_TRADE_REFUSED, reason=None,
            requested_notional_usdt=requested_notional_usdt,
        )

        try:
            inventory_guard.assert_inventory_constitution_allowed(symbol, sell_exchange, requested_notional_usdt)
        except InventoryExecutionRefused as exc:
            result.reason = str(exc)
            result.completed_at = time.time()
            return result

        inventory_guard.register_operation_start()
        try:
            quote = await self._fresh_arbitrage_quote(symbol, buy_exchange_for_arbitrage, sell_exchange, requested_notional_usdt)
            if quote is None or not quote.executable or quote.net_profit_usd <= 0:
                result.outcome = InventoryConstitutionOutcome.NO_TRADE_EDGE_INSUFFICIENT
                result.reason = (quote.reason if quote is not None else "fresh quote unavailable (data fetch failed)") or "net profit not strictly positive"
                result.pre_purchase_net_edge_usd = quote.net_profit_usd if quote is not None else None
                result.completed_at = time.time()
                return result
            result.pre_purchase_net_edge_usd = quote.net_profit_usd

            sell_market = await self._fetch_leg_snapshot(symbol, sell_exchange, "sell")
            if sell_market is None:
                result.outcome = InventoryConstitutionOutcome.NO_TRADE_EDGE_INSUFFICIENT
                result.reason = f"could not re-fetch {sell_exchange} market data for the required-qty calculation"
                result.completed_at = time.time()
                return result
            required_qty = compute_required_inventory_qty(quote.executable_qty, sell_market.taker_fee_rate, sell_market.step_size)
            result.required_base_qty = required_qty

            client_order_id = f"inventory-{attempt_id}"
            result.order_client_id = client_order_id
            result.submitted_at = time.time()
            try:
                await self._place_market_buy(sell_exchange, symbol, requested_notional_usdt, client_order_id)
            except Exception as exc:
                inventory_guard.engage_kill_switch(f"inventory buy submission raised an ambiguous error: {exc}")
                result.outcome = InventoryConstitutionOutcome.UNKNOWN
                result.reason = str(exc)
                result.completed_at = time.time()
                return result

            reached, status = await self._await_terminal(lambda: self._get_status(sell_exchange, symbol, client_order_id), LEG_CONFIRMATION_TIMEOUT_SECONDS)
            if not reached or status is None:
                inventory_guard.engage_kill_switch(f"inventory buy status unknown after timeout for attempt {attempt_id} — manual reconciliation required, no retry")
                result.outcome = InventoryConstitutionOutcome.UNKNOWN
                result.reason = "inventory buy did not reach a terminal status within the confirmation timeout"
                result.completed_at = time.time()
                return result

            result.order_exchange_id = status.order_id
            result.order_status = status.raw_status
            result.filled_qty = status.filled_qty
            result.avg_fill_price = status.avg_fill_price
            result.fee_usd = status.fee_usd
            result.confirmed_at = time.time()

            if status.filled_qty <= 0:
                result.outcome = InventoryConstitutionOutcome.NO_FILL
                result.reason = f"inventory buy ended {status.raw_status} with zero fill"
                result.completed_at = time.time()
                return result

            result.outcome = InventoryConstitutionOutcome.FILLED

            # Step 7-9: recompute the arbitrage immediately after the real
            # fill — never assume the edge that justified the purchase is
            # still there a few seconds later.
            post_quote = await self._fresh_arbitrage_quote(symbol, buy_exchange_for_arbitrage, sell_exchange, requested_notional_usdt)
            if post_quote is not None:
                result.post_fill_net_edge_usd = post_quote.net_profit_usd
                result.edge_still_valid_after_fill = post_quote.executable and post_quote.net_profit_usd > 0
            else:
                result.edge_still_valid_after_fill = False

            # ready_for_arbitrage is informational ONLY — this module
            # never calls execute_one_arbitrage itself, regardless of
            # this value (user directive: inventory purchase and
            # arbitrage execution are separately authorized actions).
            result.ready_for_arbitrage = bool(result.edge_still_valid_after_fill) and status.filled_qty >= (result.required_base_qty or 0) * 0.99

            result.completed_at = time.time()
            return result
        finally:
            inventory_guard.register_operation_end()


inventory_constitution_executor = InventoryConstitutionExecutor()
