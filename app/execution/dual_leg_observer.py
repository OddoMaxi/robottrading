"""Dual-leg orchestrator (Phase 2F, user directive, 2026-08-23) —
READ-ONLY. Fetches BOTH legs of a cross_exchange opportunity from live,
independent exchange data and computes the full arbitrage via
app.execution.dual_leg_quote.compute_dual_leg_quote. No order is placed
anywhere in this module.

Currently supports Binance <-> Bybit — the only mirror pairing this
codebase has a signed-capable client for, and empirically (see the
Phase 2F report) the only pairing ever observed for the dominant
cross_exchange strategy (LUNCUSDT: 100% Binance-buy / Bybit-sell across
2,622 historical opportunities). An opportunity whose non-Binance leg is
on an exchange without a client here (e.g. OKX) is skipped outright, not
silently approximated with Binance-shaped data.

The two legs are fetched SEQUENTIALLY, on purpose — there is no API that
snapshots two different exchanges atomically. The real wall-clock gap
between finishing leg A's fetch and finishing leg B's fetch becomes
dual_leg_latency_ms in the resulting quote, exactly the execution risk
this phase exists to measure, not hide.
"""

import logging
import time

from app.execution.binance_account_client import BinanceAccountClient, BinanceCredentialsMissing
from app.execution.binance_filters import SymbolNotFound as BinanceSymbolNotFound
from app.execution.binance_filters import parse_symbol_rules as parse_binance_symbol_rules
from app.execution.bybit_client import BybitClient, BybitCredentialsMissing
from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_dual_leg_quote
from app.opportunity.models import Opportunity

logger = logging.getLogger(__name__)

SUPPORTED_MIRROR_EXCHANGES = {"bybit"}
DEFAULT_TAKER_FEE_RATE = 0.001
SYMBOL_INFO_TTL_SECONDS = 3600.0
FEE_TTL_SECONDS = 3600.0


def _strip_slash(symbol: str) -> str:
    return symbol.replace("/", "")


def _find_binance_and_mirror_legs(opp: Opportunity) -> tuple[dict, dict] | None:
    binance_leg = next((leg for leg in opp.legs if leg.get("exchange") == "binance"), None)
    mirror_leg = next((leg for leg in opp.legs if leg.get("exchange") != "binance"), None)
    if binance_leg is None or mirror_leg is None:
        return None
    return binance_leg, mirror_leg


class DualLegObserverState:
    def __init__(self) -> None:
        self.unsupported_mirror_exchanges: dict[str, int] = {}
        self.last_error: str | None = None

    def record_unsupported(self, exchange: str) -> None:
        self.unsupported_mirror_exchanges[exchange] = self.unsupported_mirror_exchanges.get(exchange, 0) + 1


dual_leg_observer_state = DualLegObserverState()


class DualLegObserver:
    def __init__(self, binance_client: BinanceAccountClient | None = None, bybit_client: BybitClient | None = None) -> None:
        self._binance = binance_client or BinanceAccountClient()
        self._bybit = bybit_client or BybitClient()
        self._binance_rules_cache: dict[str, tuple[float, object]] = {}
        self._binance_fee_cache: dict[str, tuple[float, object]] = {}
        self._bybit_rules_cache: dict[str, tuple[float, object]] = {}
        self._bybit_fee_cache: dict[str, tuple[float, object]] = {}

    async def _binance_leg_snapshot(self, symbol: str, side: str, now: float) -> LegSnapshot | None:
        started = time.time()
        try:
            book = await self._binance.get_book_ticker(symbol)
            best_bid, best_ask = float(book["bidPrice"]), float(book["askPrice"])

            cached_rules = self._binance_rules_cache.get(symbol)
            if cached_rules is not None and now - cached_rules[0] < SYMBOL_INFO_TTL_SECONDS:
                rules = cached_rules[1]
            else:
                info = await self._binance.get_exchange_info(symbols=[symbol])
                rules = parse_binance_symbol_rules(info, symbol)
                self._binance_rules_cache[symbol] = (now, rules)

            cached_fee = self._binance_fee_cache.get(symbol)
            if cached_fee is not None and now - cached_fee[0] < FEE_TTL_SECONDS:
                fee = cached_fee[1]
            else:
                try:
                    fee = await self._binance.get_trade_fee(symbol)
                except BinanceCredentialsMissing:
                    fee = None
                self._binance_fee_cache[symbol] = (now, fee)

            depth = await self._binance.get_order_book_depth(symbol, limit=20)
            side_key = "asks" if side == "buy" else "bids"
            depth_levels = [(float(p), float(q)) for p, q in depth.get(side_key, [])]

            taker_fee_rate = fee.taker_fee_rate if fee is not None else DEFAULT_TAKER_FEE_RATE
            maker_fee_rate = fee.maker_fee_rate if fee is not None else None
            fee_source = "real_account_fee" if fee is not None else "estimated_default"

            completed = time.time()
            return LegSnapshot(
                exchange="binance",
                side=side,
                best_bid=best_bid,
                best_ask=best_ask,
                depth_levels=depth_levels,
                min_qty=rules.min_qty,
                step_size=rules.step_size,
                tick_size=rules.tick_size,
                min_notional=rules.min_notional,
                tradable=(rules.status == "TRADING" and rules.is_spot_trading_allowed),
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                fee_source=fee_source,
                fetch_started_at=started,
                fetch_completed_at=completed,
            )
        except BinanceSymbolNotFound:
            return None
        except Exception as exc:
            logger.warning("dual-leg: binance leg fetch failed for %s: %s", symbol, exc)
            return None

    async def _bybit_leg_snapshot(self, symbol: str, side: str, now: float) -> LegSnapshot | None:
        started = time.time()
        try:
            book = await self._bybit.get_book_ticker(symbol)
            if book is None:
                return None
            best_bid, best_ask = book.bid_price, book.ask_price

            cached_rules = self._bybit_rules_cache.get(symbol)
            if cached_rules is not None and now - cached_rules[0] < SYMBOL_INFO_TTL_SECONDS:
                rules = cached_rules[1]
            else:
                rules = await self._bybit.get_symbol_rules(symbol)
                self._bybit_rules_cache[symbol] = (now, rules)
            if rules is None:
                return None

            cached_fee = self._bybit_fee_cache.get(symbol)
            if cached_fee is not None and now - cached_fee[0] < FEE_TTL_SECONDS:
                fee = cached_fee[1]
            else:
                try:
                    fee = await self._bybit.get_fee_rate(symbol)
                except BybitCredentialsMissing:
                    fee = None
                self._bybit_fee_cache[symbol] = (now, fee)

            depth = await self._bybit.get_order_book_depth(symbol, limit=50)
            raw_levels = depth.get("result", {}).get("a" if side == "buy" else "b", [])
            depth_levels = [(float(p), float(q)) for p, q in raw_levels]

            taker_fee_rate = fee.taker_fee_rate if fee is not None else DEFAULT_TAKER_FEE_RATE
            maker_fee_rate = fee.maker_fee_rate if fee is not None else None
            fee_source = "real_account_fee" if fee is not None else "estimated_default"

            completed = time.time()
            return LegSnapshot(
                exchange="bybit",
                side=side,
                best_bid=best_bid,
                best_ask=best_ask,
                depth_levels=depth_levels,
                min_qty=rules.min_order_qty,
                step_size=rules.qty_step,
                tick_size=rules.tick_size,
                min_notional=rules.min_order_amt,
                tradable=rules.is_tradable,
                maker_fee_rate=maker_fee_rate,
                taker_fee_rate=taker_fee_rate,
                fee_source=fee_source,
                fetch_started_at=started,
                fetch_completed_at=completed,
            )
        except Exception as exc:
            logger.warning("dual-leg: bybit leg fetch failed for %s: %s", symbol, exc)
            return None

    async def observe(self, opp: Opportunity, micro_live_cap_usdt: float, now: float | None = None) -> DualLegQuote | None:
        """Best-effort, never raises. Returns None if the opportunity
        isn't a Binance<->supported-mirror pair, or any leg's real data
        couldn't be fetched — an absence here is one fewer observation,
        never a paper-execution-affecting error."""
        legs = _find_binance_and_mirror_legs(opp)
        if legs is None:
            return None
        binance_leg_raw, mirror_leg_raw = legs
        mirror_exchange = mirror_leg_raw.get("exchange")
        if mirror_exchange not in SUPPORTED_MIRROR_EXCHANGES:
            dual_leg_observer_state.record_unsupported(str(mirror_exchange))
            return None

        symbol = _strip_slash(opp.symbol)
        now = now if now is not None else time.time()

        binance_snapshot = await self._binance_leg_snapshot(symbol, binance_leg_raw.get("side", "buy"), now)
        if binance_snapshot is None:
            return None
        mirror_snapshot = await self._bybit_leg_snapshot(symbol, mirror_leg_raw.get("side", "sell"), time.time())
        if mirror_snapshot is None:
            return None

        buy_leg, sell_leg = (binance_snapshot, mirror_snapshot) if binance_leg_raw.get("side") == "buy" else (mirror_snapshot, binance_snapshot)

        try:
            return compute_dual_leg_quote(
                opportunity_id=opp.id,
                symbol=symbol,
                buy_leg=buy_leg,
                sell_leg=sell_leg,
                master_requested_size_usd=opp.capital_usd or 0.0,
                micro_live_cap_usdt=micro_live_cap_usdt,
                now=time.time(),
            )
        except Exception as exc:
            logger.warning("dual-leg: quote computation failed for %s: %s", symbol, exc)
            return None


dual_leg_observer = DualLegObserver()
