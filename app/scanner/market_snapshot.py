"""Per-(exchange, symbol) live market snapshot fetching (user directive,
2026-08-23) — READ-ONLY. Feeds app.scanner.cross_exchange_scanner, which
reuses app.execution.dual_leg_quote.compute_dual_leg_quote (already
exchange-agnostic) for the actual arbitrage math — this module's only
job is turning three different exchanges' native API shapes into one
common SymbolMarketData shape.
"""

import logging
import time
from dataclasses import dataclass

from app.execution.binance_account_client import BinanceAccountClient, BinanceCredentialsMissing
from app.execution.binance_filters import SymbolNotFound as BinanceSymbolNotFound
from app.execution.binance_filters import parse_symbol_rules as parse_binance_symbol_rules
from app.execution.bybit_client import BybitClient, BybitCredentialsMissing
from app.execution.reality_quote import DEFAULT_TAKER_FEE_RATE
from app.scanner.okx_public_client import OKX_ESTIMATED_MAKER_FEE_RATE, OKX_ESTIMATED_TAKER_FEE_RATE, OkxPublicClient, to_okx_symbol

logger = logging.getLogger(__name__)

EXCHANGES = ("binance", "bybit", "okx")


@dataclass(slots=True)
class SymbolMarketData:
    exchange: str
    symbol: str
    best_bid: float
    best_ask: float
    ask_depth: list[tuple[float, float]]
    bid_depth: list[tuple[float, float]]
    min_qty: float
    step_size: float
    tick_size: float
    min_notional: float | None
    tradable: bool
    maker_fee_rate: float | None
    taker_fee_rate: float
    fee_source: str
    fetched_at: float


class MultiExchangeSnapshotFetcher:
    """TTL-cached fetchers per exchange — filters/fees change rarely,
    book/depth must be fetched fresh every scan cycle."""

    SYMBOL_INFO_TTL_SECONDS = 3600.0
    FEE_TTL_SECONDS = 3600.0

    def __init__(
        self,
        binance: BinanceAccountClient | None = None,
        bybit: BybitClient | None = None,
        okx: OkxPublicClient | None = None,
    ) -> None:
        self._binance = binance or BinanceAccountClient()
        self._bybit = bybit or BybitClient()
        self._okx = okx or OkxPublicClient()
        self._rules_cache: dict[tuple[str, str], tuple[float, object]] = {}
        self._fee_cache: dict[tuple[str, str], tuple[float, object]] = {}

    async def fetch(self, exchange: str, symbol: str) -> SymbolMarketData | None:
        now = time.time()
        try:
            if exchange == "binance":
                return await self._fetch_binance(symbol, now)
            if exchange == "bybit":
                return await self._fetch_bybit(symbol, now)
            if exchange == "okx":
                return await self._fetch_okx(symbol, now)
        except Exception as exc:
            logger.debug("scanner: %s/%s fetch failed: %s", exchange, symbol, exc)
            return None
        raise ValueError(f"unknown exchange {exchange}")

    async def _fetch_binance(self, symbol: str, now: float) -> SymbolMarketData | None:
        binance_symbol = symbol.replace("/", "")
        book = await self._binance.get_book_ticker(binance_symbol)
        depth = await self._binance.get_order_book_depth(binance_symbol, limit=20)

        cache_key = ("binance", binance_symbol)
        cached_rules = self._rules_cache.get(cache_key)
        if cached_rules is not None and now - cached_rules[0] < self.SYMBOL_INFO_TTL_SECONDS:
            rules = cached_rules[1]
        else:
            try:
                info = await self._binance.get_exchange_info(symbols=[binance_symbol])
                rules = parse_binance_symbol_rules(info, binance_symbol)
            except BinanceSymbolNotFound:
                return None
            self._rules_cache[cache_key] = (now, rules)

        cached_fee = self._fee_cache.get(cache_key)
        if cached_fee is not None and now - cached_fee[0] < self.FEE_TTL_SECONDS:
            fee = cached_fee[1]
        else:
            try:
                fee = await self._binance.get_trade_fee(binance_symbol)
            except BinanceCredentialsMissing:
                fee = None
            self._fee_cache[cache_key] = (now, fee)

        return SymbolMarketData(
            exchange="binance",
            symbol=symbol,
            best_bid=float(book["bidPrice"]),
            best_ask=float(book["askPrice"]),
            ask_depth=[(float(p), float(q)) for p, q in depth.get("asks", [])],
            bid_depth=[(float(p), float(q)) for p, q in depth.get("bids", [])],
            min_qty=rules.min_qty,
            step_size=rules.step_size,
            tick_size=rules.tick_size,
            min_notional=rules.min_notional,
            tradable=(rules.status == "TRADING" and rules.is_spot_trading_allowed),
            maker_fee_rate=fee.maker_fee_rate if fee is not None else None,
            taker_fee_rate=fee.taker_fee_rate if fee is not None else DEFAULT_TAKER_FEE_RATE,
            fee_source="real_account_fee" if fee is not None else "estimated_default",
            fetched_at=time.time(),
        )

    async def _fetch_bybit(self, symbol: str, now: float) -> SymbolMarketData | None:
        bybit_symbol = symbol.replace("/", "")
        book = await self._bybit.get_book_ticker(bybit_symbol)
        if book is None:
            return None
        depth = await self._bybit.get_order_book_depth(bybit_symbol, limit=50)

        cache_key = ("bybit", bybit_symbol)
        cached_rules = self._rules_cache.get(cache_key)
        if cached_rules is not None and now - cached_rules[0] < self.SYMBOL_INFO_TTL_SECONDS:
            rules = cached_rules[1]
        else:
            rules = await self._bybit.get_symbol_rules(bybit_symbol)
            self._rules_cache[cache_key] = (now, rules)
        if rules is None:
            return None

        cached_fee = self._fee_cache.get(cache_key)
        if cached_fee is not None and now - cached_fee[0] < self.FEE_TTL_SECONDS:
            fee = cached_fee[1]
        else:
            try:
                fee = await self._bybit.get_fee_rate(bybit_symbol)
            except BybitCredentialsMissing:
                fee = None
            self._fee_cache[cache_key] = (now, fee)

        return SymbolMarketData(
            exchange="bybit",
            symbol=symbol,
            best_bid=book.bid_price,
            best_ask=book.ask_price,
            ask_depth=[(float(p), float(q)) for p, q in depth.get("result", {}).get("a", [])],
            bid_depth=[(float(p), float(q)) for p, q in depth.get("result", {}).get("b", [])],
            min_qty=rules.min_order_qty,
            step_size=rules.qty_step,
            tick_size=rules.tick_size,
            min_notional=rules.min_order_amt,
            tradable=rules.is_tradable,
            maker_fee_rate=fee.maker_fee_rate if fee is not None else None,
            taker_fee_rate=fee.taker_fee_rate if fee is not None else DEFAULT_TAKER_FEE_RATE,
            fee_source="real_account_fee" if fee is not None else "estimated_default",
            fetched_at=time.time(),
        )

    async def _fetch_okx(self, symbol: str, now: float) -> SymbolMarketData | None:
        okx_symbol = to_okx_symbol(symbol)
        book = await self._okx.get_book_ticker(symbol)
        if book is None:
            return None
        depth = await self._okx.get_order_book_depth(symbol, limit=20)

        cache_key = ("okx", okx_symbol)
        cached_rules = self._rules_cache.get(cache_key)
        if cached_rules is not None and now - cached_rules[0] < self.SYMBOL_INFO_TTL_SECONDS:
            rules = cached_rules[1]
        else:
            rules = await self._okx.get_symbol_rules(symbol)
            self._rules_cache[cache_key] = (now, rules)
        if rules is None:
            return None

        book_data = depth.get("data", [{}])
        levels = book_data[0] if book_data else {}
        return SymbolMarketData(
            exchange="okx",
            symbol=symbol,
            best_bid=book.bid_price,
            best_ask=book.ask_price,
            ask_depth=[(float(p), float(q)) for p, q, *_ in levels.get("asks", [])],
            bid_depth=[(float(p), float(q)) for p, q, *_ in levels.get("bids", [])],
            min_qty=rules.min_qty,
            step_size=rules.lot_size,
            tick_size=rules.tick_size,
            min_notional=None,  # OKX spot publishes no separate notional floor; minSz (base asset) is the binding constraint
            tradable=rules.is_tradable,
            maker_fee_rate=OKX_ESTIMATED_MAKER_FEE_RATE,
            taker_fee_rate=OKX_ESTIMATED_TAKER_FEE_RATE,
            fee_source="estimated_default",  # no okx_api_key configured — never fabricated as real
            fetched_at=time.time(),
        )
