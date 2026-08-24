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
from app.execution.binance_filters import round_down_to_step
from app.execution.binance_live_trade_client import BinanceLiveTradeClient, aggregate_binance_trades
from app.execution.bybit_client import BybitClient
from app.execution.bybit_live_trade_client import BybitLiveTradeClient
from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_dual_leg_quote
from app.execution.inventory_guard import InventoryExecutionRefused, inventory_guard
from app.execution.reality_quote import DEFAULT_TAKER_FEE_RATE

logger = logging.getLogger(__name__)

LEG_CONFIRMATION_TIMEOUT_SECONDS = 15.0
LEG_POLL_INTERVAL_SECONDS = 0.5
EXCHANGES = ("binance", "bybit")
QUOTE_ASSET = "USDT"  # every symbol in this codebase is USDT-quoted (app.execution.inventory_manager's own convention)

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
    filled_qty: float = 0.0  # GROSS, exactly as reported by the exchange (cumExecQty) — never reduced for fees
    # ACTUALLY held after the fill (2026-08-24 fix, after the first real
    # Bybit fill reported 2917.9 RVN filled but the wallet only held
    # 2914.9821 — the 2.9179 RVN fee was never subtracted anywhere).
    # This, not filled_qty, is what a subsequent arbitrage can safely use.
    net_filled_qty: float = 0.0
    avg_fill_price: float | None = None
    # Never assume a Bybit/Binance fee is in USDT — fee_asset is read
    # directly from the exchange's own per-asset fee breakdown.
    fee_asset: str | None = None
    fee_amount: float | None = None  # raw amount, in fee_asset units
    fee_usd_equivalent: float | None = None  # None when the conversion could not be safely determined (unrecognized fee asset)
    fee_usd: float | None = None  # backward-compat alias — always equal to fee_usd_equivalent from this fix onward
    submitted_at: float | None = None
    confirmed_at: float | None = None

    post_fill_net_edge_usd: float | None = None
    edge_still_valid_after_fill: bool | None = None
    # FIX 2 (user directive, 2026-08-24): requested_notional_usdt is a
    # CAP on the eventual arbitrage, never a mandatory size — this is the
    # actual largest notional the REAL net_filled_qty can safely support
    # right now (0.0 if none, e.g. below the sell exchange's own
    # MIN_NOTIONAL). The caller should request exactly this notional
    # from live_arbitrage_executor.execute_one_arbitrage, not blindly
    # reuse requested_notional_usdt.
    max_safe_arbitrage_notional_usdt: float | None = None
    ready_for_arbitrage: bool = False

    started_at: float = field(default_factory=time.time)
    completed_at: float | None = None


@dataclass(slots=True)
class _NormalizedFillStatus:
    order_id: str
    is_terminal: bool
    is_filled: bool
    filled_qty: float  # GROSS — as reported by the exchange
    net_base_qty: float  # ACTUALLY held after the fill — see _net_base_qty
    avg_fill_price: float | None
    fee_asset: str | None
    fee_amount: float
    fee_usd_equivalent: float | None
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


def resolve_fee(fees_by_asset: dict[str, float], base_asset: str, avg_fill_price: float | None, log_prefix: str) -> tuple[str | None, float, float | None]:
    """Pure function. Returns (fee_asset, fee_amount, fee_usd_equivalent)
    — NEVER assumes a currency (2026-08-24 fix, after the first real
    Bybit fill mislabeled a 2.9179 RVN fee as $2.9179). fees_by_asset is
    an {asset: amount} map exactly as Bybit's cumFeeDetail / Binance's
    per-fill commission already report per order — the overwhelmingly
    common case for one spot market order is exactly one entry.

    fee_usd_equivalent is:
      - the raw amount, if the fee was charged in the quote asset (USDT);
      - the raw amount * avg_fill_price, if charged in the symbol's own
        base asset (this order's own fill price IS the USDT-per-base-unit
        rate, so no external price lookup is needed for THIS case);
      - None otherwise (an asset unrelated to this symbol, or more than
        one fee asset on a single order) — never guessed."""
    nonzero = {asset: amount for asset, amount in fees_by_asset.items() if amount}
    if not nonzero:
        return None, 0.0, 0.0
    if len(nonzero) > 1:
        logger.warning("%s: order fee charged in multiple assets, cannot compute a single USD equivalent: %s", log_prefix, nonzero)
        return None, 0.0, None
    (fee_asset, fee_amount), = nonzero.items()
    if fee_asset == QUOTE_ASSET:
        return fee_asset, fee_amount, fee_amount
    if fee_asset == base_asset and avg_fill_price is not None and avg_fill_price > 0:
        return fee_asset, fee_amount, fee_amount * avg_fill_price
    logger.warning("%s: order fee charged in unrecognized asset %s, cannot compute a USD equivalent without a price for it", log_prefix, fee_asset)
    return fee_asset, fee_amount, None


def net_base_qty_after_fee(gross_qty: float, fee_asset: str | None, fee_amount: float, base_asset: str) -> float:
    """Pure function. The base-asset quantity actually held after a
    fill — only reduced by the fee when the fee itself was charged in
    that SAME base asset. A quote-asset (or any other) fee does not
    reduce how much of the base asset was received."""
    if fee_asset == base_asset:
        return max(0.0, gross_qty - fee_amount)
    return gross_qty


def compute_max_safe_notional(
    available_base_qty: float, sell_price: float, max_notional_usdt: float, sell_min_notional: float, sell_step_size: float
) -> float:
    """Pure function (FIX 2, user directive, 2026-08-24). max_notional_usdt
    is a CAP, never a mandatory size — never reject a smaller,
    genuinely-tradable opportunity just because it cannot hit the cap
    exactly. Returns the largest notional that (a) never exceeds
    available_base_qty's real USDT value at sell_price, (b) never
    exceeds the hard cap, and (c) still clears the sell exchange's own
    MIN_NOTIONAL floor — 0.0 if even the full available quantity can't
    clear it (genuinely too little to trade, not a bug)."""
    if available_base_qty <= 0 or sell_price <= 0:
        return 0.0
    tradable_qty = round_down_to_step(available_base_qty, sell_step_size) if sell_step_size > 0 else available_base_qty
    safe_notional = min(tradable_qty * sell_price, max_notional_usdt)
    if safe_notional < sell_min_notional:
        return 0.0
    return safe_notional


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

    async def _fresh_arbitrage_quote(
        self, symbol: str, buy_exchange: str, sell_exchange: str, notional_usdt: float
    ) -> tuple[DualLegQuote, LegSnapshot, LegSnapshot] | None:
        """Returns (quote, buy_leg, sell_leg) — the leg snapshots are
        needed by FIX 2's compute_max_safe_notional (sell_leg.best_bid/
        min_notional/step_size), not just the quote itself."""
        try:
            buy_leg = await self._fetch_leg_snapshot(symbol, buy_exchange, "buy")
            sell_leg = await self._fetch_leg_snapshot(symbol, sell_exchange, "sell")
            if buy_leg is None or sell_leg is None:
                return None
            quote = compute_dual_leg_quote(
                opportunity_id=uuid.uuid4(), symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg,
                master_requested_size_usd=notional_usdt, micro_live_cap_usdt=notional_usdt,
            )
            return quote, buy_leg, sell_leg
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
        base_asset = symbol.removesuffix(QUOTE_ASSET)
        if exchange == "binance":
            result = await self._binance_trade.get_order_status(symbol, orig_client_order_id=client_order_id)
            if not result.is_terminal or result.executed_qty <= 0:
                # Not yet resolved, or resolved with zero fill -- no real
                # trades to fetch; get_order_status's own qty/status are
                # already reliable for this case (there is no fee to mis-report).
                return _NormalizedFillStatus(
                    order_id=str(result.order_id), is_terminal=result.is_terminal, is_filled=result.is_filled,
                    filled_qty=result.executed_qty, net_base_qty=result.executed_qty,
                    avg_fill_price=result.average_fill_price(),
                    fee_asset=None, fee_amount=0.0, fee_usd_equivalent=0.0,
                    raw_status=result.status,
                )
            # FIX 1 (user directive, 2026-08-24, after a real SAND
            # inventory buy on Binance charged 0.237 SAND in fees that
            # this branch silently missed -- get_order_status's own
            # single-order endpoint never returns fills at all; the same
            # fix already applied to live_arbitrage_executor's own
            # _get_status, mirrored here: fetch the REAL trades via
            # get_order_trades (the account trade-history endpoint) once
            # the order is terminal and filled.
            trades = await self._binance_trade.get_order_trades(symbol, result.order_id)
            aggregated = aggregate_binance_trades(trades)
            gross_qty = aggregated.gross_base_qty if trades else result.executed_qty
            avg_price = aggregated.actual_effective_price if trades else result.average_fill_price()
            fee_asset, fee_amount, fee_usd_equivalent = resolve_fee(aggregated.fees_by_asset, base_asset, avg_price, "inventory-constitution")
            return _NormalizedFillStatus(
                order_id=str(result.order_id), is_terminal=result.is_terminal, is_filled=result.is_filled,
                filled_qty=gross_qty, net_base_qty=net_base_qty_after_fee(gross_qty, fee_asset, fee_amount, base_asset),
                avg_fill_price=avg_price, fee_asset=fee_asset, fee_amount=fee_amount, fee_usd_equivalent=fee_usd_equivalent,
                raw_status=result.status,
            )
        else:
            status = await self._bybit_trade.get_order_status(symbol, order_link_id=client_order_id)
            if status is None:
                return None
            gross_qty = status.cum_exec_qty
            fee_asset, fee_amount, fee_usd_equivalent = resolve_fee(status.total_fees_by_asset(), base_asset, status.avg_price, "inventory-constitution")
            return _NormalizedFillStatus(
                order_id=status.order_id, is_terminal=status.is_terminal, is_filled=status.is_filled,
                filled_qty=gross_qty, net_base_qty=net_base_qty_after_fee(gross_qty, fee_asset, fee_amount, base_asset),
                avg_fill_price=status.avg_price, fee_asset=fee_asset, fee_amount=fee_amount, fee_usd_equivalent=fee_usd_equivalent,
                raw_status=status.order_status,
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
            fresh = await self._fresh_arbitrage_quote(symbol, buy_exchange_for_arbitrage, sell_exchange, requested_notional_usdt)
            if fresh is None or not fresh[0].executable or fresh[0].net_profit_usd <= 0:
                result.outcome = InventoryConstitutionOutcome.NO_TRADE_EDGE_INSUFFICIENT
                result.reason = (fresh[0].reason if fresh is not None else "fresh quote unavailable (data fetch failed)") or "net profit not strictly positive"
                result.pre_purchase_net_edge_usd = fresh[0].net_profit_usd if fresh is not None else None
                result.completed_at = time.time()
                return result
            quote, _buy_market, sell_market = fresh
            result.pre_purchase_net_edge_usd = quote.net_profit_usd

            required_qty = compute_required_inventory_qty(quote.executable_qty, sell_market.taker_fee_rate, sell_market.step_size)
            result.required_base_qty = required_qty

            # Bybit's orderLinkId has a documented 36-character max — a
            # full "inventory-{uuid4}" (46 chars) silently exceeded it
            # and is the prime suspect for the real retCode=170003
            # "unknown parameter" rejections (2026-08-24). attempt_id.hex
            # (32 hex chars, no hyphens) truncated to 24 keeps this well
            # under the limit while remaining effectively collision-proof
            # at max_concurrent_inventory_operations=1.
            client_order_id = f"inv-{attempt_id.hex[:24]}"
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
            result.net_filled_qty = status.net_base_qty
            result.avg_fill_price = status.avg_fill_price
            result.fee_asset = status.fee_asset
            result.fee_amount = status.fee_amount
            result.fee_usd_equivalent = status.fee_usd_equivalent
            result.fee_usd = status.fee_usd_equivalent  # backward-compat alias, now correctly currency-aware
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
            post_fresh = await self._fresh_arbitrage_quote(symbol, buy_exchange_for_arbitrage, sell_exchange, requested_notional_usdt)
            if post_fresh is not None:
                post_quote, _post_buy_market, post_sell_market = post_fresh
                result.post_fill_net_edge_usd = post_quote.net_profit_usd
                result.edge_still_valid_after_fill = post_quote.executable and post_quote.net_profit_usd > 0
                # FIX 2 (user directive, 2026-08-24): requested_notional_usdt
                # is a CAP, not a mandatory size — never reject a smaller,
                # still-profitable arbitrage just because net_filled_qty
                # (reduced by a base-asset fee, or simply capped by the
                # same MAX_NOTIONAL used for this purchase) can't cover a
                # FULL requested_notional_usdt-sized trade after
                # compute_required_inventory_qty's own margin.
                result.max_safe_arbitrage_notional_usdt = compute_max_safe_notional(
                    result.net_filled_qty, post_sell_market.best_bid, requested_notional_usdt,
                    post_sell_market.min_notional, post_sell_market.step_size,
                )
            else:
                result.edge_still_valid_after_fill = False
                result.max_safe_arbitrage_notional_usdt = 0.0

            # ready_for_arbitrage is informational ONLY — this module
            # never calls execute_one_arbitrage itself, regardless of
            # this value (user directive: inventory purchase and
            # arbitrage execution are separately authorized actions). The
            # caller should request max_safe_arbitrage_notional_usdt from
            # live_arbitrage_executor.execute_one_arbitrage, not blindly
            # reuse requested_notional_usdt.
            result.ready_for_arbitrage = bool(result.edge_still_valid_after_fill) and (result.max_safe_arbitrage_notional_usdt or 0) > 0

            result.completed_at = time.time()
            return result
        finally:
            inventory_guard.register_operation_end()


inventory_constitution_executor = InventoryConstitutionExecutor()
