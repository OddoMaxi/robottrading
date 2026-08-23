"""Cross-exchange scan cycle (user directive, 2026-08-23) — READ-ONLY.

For one symbol, fetches live market data from every reachable exchange
and evaluates ALL ordered exchange-pair directions (buy X, sell Y for
every X != Y) by reusing app.execution.dual_leg_quote.compute_dual_leg_quote
— the exact same, already-validated math Phase 2F/3A built for
LUNCUSDT/Binance/Bybit, applied generically here. No order is placed;
this module only computes and returns data.
"""

import time
import uuid
from dataclasses import dataclass

from app.execution.dual_leg_quote import DualLegQuote, LegSnapshot, compute_dual_leg_quote
from app.scanner.market_snapshot import EXCHANGES, MultiExchangeSnapshotFetcher, SymbolMarketData

REFERENCE_NOTIONAL_USD = 1000.0  # this scanner's own comparison size — distinct from Phase 2D-3A's 10 USDT micro-live cap


def _leg_snapshot(data: SymbolMarketData, side: str) -> LegSnapshot:
    depth = data.ask_depth if side == "buy" else data.bid_depth
    return LegSnapshot(
        exchange=data.exchange,
        side=side,
        best_bid=data.best_bid,
        best_ask=data.best_ask,
        depth_levels=depth,
        min_qty=data.min_qty,
        step_size=data.step_size,
        tick_size=data.tick_size,
        min_notional=data.min_notional,
        tradable=data.tradable,
        maker_fee_rate=data.maker_fee_rate,
        taker_fee_rate=data.taker_fee_rate,
        fee_source=data.fee_source,
        fetch_started_at=data.fetched_at,
        fetch_completed_at=data.fetched_at,
    )


@dataclass(slots=True)
class DirectionQuote:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    quote: DualLegQuote


async def scan_symbol(
    fetcher: MultiExchangeSnapshotFetcher,
    symbol: str,
    reference_notional_usd: float = REFERENCE_NOTIONAL_USD,
    exchanges: tuple[str, ...] = EXCHANGES,
) -> list[DirectionQuote]:
    """Fetches every requested exchange once (not once per direction) and
    returns a DirectionQuote for every ordered pair where BOTH sides were
    reachable — an exchange where the symbol isn't listed or fails to
    fetch is silently skipped for that pair ('if available', per the
    directive), never faked. `exchanges` defaults to all three
    (Binance/Bybit/OKX, the altcoin scanner's own scope); callers that
    only trade a subset (e.g. app.execution.live_ranker, Binance+Bybit
    only — no OKX live-trade client exists) pass a narrower tuple."""
    snapshots: dict[str, SymbolMarketData] = {}
    for exchange in exchanges:
        data = await fetcher.fetch(exchange, symbol)
        if data is not None:
            snapshots[exchange] = data

    results: list[DirectionQuote] = []
    now = time.time()
    for buy_exchange, buy_data in snapshots.items():
        for sell_exchange, sell_data in snapshots.items():
            if buy_exchange == sell_exchange:
                continue
            buy_leg = _leg_snapshot(buy_data, "buy")
            sell_leg = _leg_snapshot(sell_data, "sell")
            quote = compute_dual_leg_quote(
                opportunity_id=uuid.uuid4(),
                symbol=symbol,
                buy_leg=buy_leg,
                sell_leg=sell_leg,
                master_requested_size_usd=reference_notional_usd,
                micro_live_cap_usdt=reference_notional_usd,
                now=now,
            )
            results.append(DirectionQuote(symbol=symbol, buy_exchange=buy_exchange, sell_exchange=sell_exchange, quote=quote))
    return results
