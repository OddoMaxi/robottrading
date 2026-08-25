"""LIVE OPPORTUNITY RANKER (Phase 3, user directive, 2026-08-23; extended
2026-08-25, "integre OKX aussi" / V5 three-exchange shadow) — READ-ONLY.
MASTER's continuous ranking of every qualified real-capital candidate
across the dynamically-discovered 3-exchange universe.

Reuses app.scanner.cross_exchange_scanner.scan_symbol (already
exchange-agnostic, already validated math) across all three exchanges —
this module's own job is turning those quotes into a REAL-CAPITAL-AWARE
ranking: for every candidate, checking whether the required balances are
actually pre-positioned on the right exchange RIGHT NOW (item 5 of the
original directive) and combining that with profit/liquidity/
persistence/latency into one score. Never fetches a balance to size an
order for execution — that remains app.execution.live_arbitrage_
executor's own fresh re-check, immediately before submission.

Ranking real balances against OKX uses app.execution.okx_account_client.
OkxAccountClient (read-only) only — never okx_live_trade_client, which
stays unreachable from this module exactly like every other file under
app/ (tests/test_phase3a_isolation.py).

No order is placed anywhere in this module."""

import logging
import time
from dataclasses import dataclass

from app.execution.binance_account_client import BinanceAccountClient
from app.execution.bybit_client import BybitClient, parse_wallet_balance
from app.execution.dual_leg_quote import DualLegQuote
from app.execution.live_universe import LiveUniverseBuilder, live_universe_builder
from app.execution.okx_account_client import OkxAccountClient
from app.scanner.cross_exchange_scanner import DirectionQuote, scan_symbol
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher

logger = logging.getLogger(__name__)

LIVE_EXCHANGES = ("binance", "bybit", "okx")


@dataclass(slots=True)
class PrePositioningCheck:
    buy_exchange: str
    sell_exchange: str
    required_buy_balance_usdt: float
    required_sell_asset: str
    required_sell_qty: float
    available_buy_balance_usdt: float
    available_sell_balance: float
    prepositioned: bool
    executable_now: bool


@dataclass(slots=True)
class RankedOpportunity:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    quote: DualLegQuote
    prepositioning: PrePositioningCheck
    score: float


def _base_asset(symbol: str) -> str:
    return symbol.split("/")[0]


def check_prepositioning(
    quote: DualLegQuote,
    buy_exchange: str,
    sell_exchange: str,
    base_asset: str,
    available_usdt: dict[str, float],
    available_base: dict[str, float],
) -> PrePositioningCheck:
    """available_usdt/available_base are keyed by exchange name (any of
    LIVE_EXCHANGES) -- generalized from the original 2-exchange
    binance_usdt/bybit_usdt/binance_base/bybit_base keyword arguments so
    a third (or Nth) exchange never needs another set of positional
    parameters bolted on."""
    required_buy_balance_usdt = quote.executable_qty * quote.buy_execution_price
    required_sell_qty = quote.executable_qty
    available_buy_balance_usdt = available_usdt.get(buy_exchange, 0.0)
    available_sell_balance = available_base.get(sell_exchange, 0.0)

    prepositioned = available_buy_balance_usdt >= required_buy_balance_usdt and available_sell_balance >= required_sell_qty
    return PrePositioningCheck(
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        required_buy_balance_usdt=required_buy_balance_usdt,
        required_sell_asset=base_asset,
        required_sell_qty=required_sell_qty,
        available_buy_balance_usdt=available_buy_balance_usdt,
        available_sell_balance=available_sell_balance,
        prepositioned=prepositioned,
        executable_now=prepositioned and quote.executable,
    )


def score_opportunity(quote: DualLegQuote, prepositioning: PrePositioningCheck) -> float:
    """MASTER's ranking formula (item 4): profit net attendu, profondeur
    du carnet, latence, capital nécessaire, pré-positionnement — combined
    into one number, HARD-gated to zero when capital isn't actually
    pre-positioned (an opportunity that looks great but can't be acted on
    right now must never outrank one that can)."""
    if not prepositioning.executable_now:
        return 0.0
    if quote.net_profit_usd <= 0:
        return 0.0
    # Net profit is the primary driver; a latency penalty discounts
    # opportunities whose dual-leg confirmation historically takes longer
    # (more time for the spread to move against the second leg) — both
    # already real, measured figures from the fresh quote, never invented.
    latency_penalty = 1.0 / (1.0 + quote.dual_leg_latency_ms / 1000.0)
    return quote.net_profit_usd * latency_penalty


async def rank_live_opportunities(
    universe_builder: LiveUniverseBuilder | None = None,
    binance_read: BinanceAccountClient | None = None,
    bybit_read: BybitClient | None = None,
    okx_read: OkxAccountClient | None = None,
    fetcher: MultiExchangeSnapshotFetcher | None = None,
    requested_notional_per_leg_usdt: float = 5.0,
    max_symbols: int | None = None,
) -> list[RankedOpportunity]:
    universe_builder = universe_builder or live_universe_builder
    binance_read = binance_read or BinanceAccountClient()
    bybit_read = bybit_read or BybitClient()
    okx_read = okx_read or OkxAccountClient()
    fetcher = fetcher or MultiExchangeSnapshotFetcher(binance=binance_read, bybit=bybit_read, okx_account=okx_read)

    universe = await universe_builder.get_universe()
    # Union of every pairwise-relevant symbol -- a symbol tradable only
    # on Binance+OKX (not Bybit), or Bybit+OKX (not Binance), was
    # previously invisible when this only iterated common_symbols
    # (Binance ∩ Bybit). scan_symbol itself skips any exchange a given
    # symbol genuinely isn't listed on, so including the broader union
    # here costs nothing when a symbol is only on two exchanges.
    all_symbols = sorted(set(universe.common_symbols) | set(universe.binance_okx_symbols) | set(universe.bybit_okx_symbols))
    symbols = all_symbols[:max_symbols] if max_symbols else all_symbols

    binance_usdt = bybit_usdt = okx_usdt = 0.0
    wallet = None
    try:
        snapshot = await binance_read.get_account_snapshot()
        binance_usdt = snapshot.balance_usdt() if snapshot is not None else 0.0
    except Exception as exc:
        logger.warning("live-ranker: Binance balance fetch failed: %s", exc)
    try:
        wallet = await bybit_read.get_wallet_balance()
        bybit_usdt = parse_wallet_balance(wallet, "USDT")
    except Exception as exc:
        logger.warning("live-ranker: Bybit balance fetch failed: %s", exc)
    try:
        okx_snapshot = await okx_read.get_account_snapshot()
        okx_usdt = okx_snapshot.balance_usdt() if okx_snapshot is not None else 0.0
    except Exception as exc:
        logger.warning("live-ranker: OKX balance fetch failed: %s", exc)

    ranked: list[RankedOpportunity] = []
    for symbol in symbols:
        try:
            direction_quotes = await scan_symbol(fetcher, symbol, reference_notional_usd=requested_notional_per_leg_usdt, exchanges=LIVE_EXCHANGES)
        except Exception as exc:
            logger.warning("live-ranker: scan failed for %s: %s", symbol, exc)
            continue

        base_asset = _base_asset(symbol)
        binance_base = bybit_base = okx_base = 0.0
        try:
            binance_snapshot = await binance_read.get_account_snapshot()
            binance_base = binance_snapshot.balance_of(base_asset) if binance_snapshot is not None else 0.0
        except Exception:
            pass
        if wallet is not None:
            bybit_base = parse_wallet_balance(wallet, base_asset)
        try:
            okx_snapshot = await okx_read.get_account_snapshot()
            okx_base = okx_snapshot.balance_of(base_asset) if okx_snapshot is not None else 0.0
        except Exception:
            pass

        available_usdt = {"binance": binance_usdt, "bybit": bybit_usdt, "okx": okx_usdt}
        available_base = {"binance": binance_base, "bybit": bybit_base, "okx": okx_base}

        for dq in direction_quotes:
            prepositioning = check_prepositioning(dq.quote, dq.buy_exchange, dq.sell_exchange, base_asset, available_usdt, available_base)
            score = score_opportunity(dq.quote, prepositioning)
            ranked.append(
                RankedOpportunity(symbol=symbol, buy_exchange=dq.buy_exchange, sell_exchange=dq.sell_exchange, quote=dq.quote, prepositioning=prepositioning, score=score)
            )

    ranked.sort(key=lambda r: r.score, reverse=True)
    return ranked
