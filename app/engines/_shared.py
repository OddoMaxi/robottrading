"""Shared spread-scanning logic for engines that compare one symbol's best
bid/ask across exchanges — Cross-Exchange (section 5) and Stablecoin
(section 4) arbitrage are the same mechanic over a different symbol universe.
"""

import time

from app.analytics.break_even import compute_break_even
from app.analytics.execution_depth import compute_depth_adjusted_edge
from app.analytics.fees import FeeEngine
from app.config.constants import NOMINAL_FAST_HOLDING_SECONDS, MarketType, Strategy
from app.execution.execution_simulator import simulate_best_execution
from app.market_data.normalizer import NormalizedQuote
from app.market_data.orderbook import OrderBookLevel, simulate_vwap
from app.market_data.store import MarketDataStore
from app.opportunity.false_opportunity_filter import check_leg_pair_sync
from app.opportunity.models import Opportunity

# Reality Engine spec, sections 7-8 — a real order book snapshot older than
# this is treated the same as not having one (falls back to the top-of-book
# approximation), rather than VWAP-pricing a trade against depth that's
# stopped reflecting the live book. depth20@100ms updates every 100ms, so
# anything past a couple of seconds means that feed has stalled.
MAX_ORDER_BOOK_AGE_SECONDS = 2.0


def _resolve_ask_levels(store: MarketDataStore, exchange: str, symbol: str, quote: NormalizedQuote, now: float) -> list[OrderBookLevel]:
    """Real multi-level depth where a fresh local order book exists for
    this (exchange, symbol) — all 3 priority exchanges as of the
    Opportunity Expansion spec, Step 2 (2026-08-21; Binance-only before
    that) — else the same single top-of-book level engines have always
    used, for whichever specific (exchange, symbol) pair doesn't have one
    (a newly-discovered asset not yet confirmed listed there, a stale feed)."""
    book = store.get_order_book(exchange, symbol)
    if book is not None and book.asks and (now - book.timestamp) <= MAX_ORDER_BOOK_AGE_SECONDS:
        return book.asks
    return [OrderBookLevel(quote.ask, quote.ask_quantity)]


def _resolve_bid_levels(store: MarketDataStore, exchange: str, symbol: str, quote: NormalizedQuote, now: float) -> list[OrderBookLevel]:
    book = store.get_order_book(exchange, symbol)
    if book is not None and book.bids and (now - book.timestamp) <= MAX_ORDER_BOOK_AGE_SECONDS:
        return book.bids
    return [OrderBookLevel(quote.bid, quote.bid_quantity)]


class QuoteSpreadScanner:
    def __init__(
        self,
        strategy: Strategy,
        symbols: list[str],
        store: MarketDataStore,
        fee_engine: FeeEngine,
        capital_usd: float,
    ) -> None:
        self.strategy = strategy
        self.symbols = symbols
        self.store = store
        self.fee_engine = fee_engine
        self.capital_usd = capital_usd

    async def scan(self) -> list[Opportunity]:
        opportunities: list[Opportunity] = []
        for symbol in self.symbols:
            quotes = self.store.quotes_for_symbol(MarketType.SPOT, symbol)
            for buy_exchange, buy_quote in quotes.items():
                for sell_exchange, sell_quote in quotes.items():
                    if buy_exchange == sell_exchange or buy_quote.ask <= 0 or sell_quote.bid <= 0:
                        continue

                    # False Opportunity Filter: reject stale or desynced
                    # quotes before treating the gap between them as real.
                    data_quality = check_leg_pair_sync(buy_quote, sell_quote, time.time())
                    if not data_quality.is_valid:
                        continue

                    gross_spread_pct = (sell_quote.bid - buy_quote.ask) / buy_quote.ask * 100
                    if gross_spread_pct <= 0:
                        continue

                    # Break-Even Engine: cheap upfront gate, fees alone are
                    # known without touching the order book. Gate on the
                    # MAKER/MAKER break-even — the lowest of the 4 execution
                    # modes' thresholds — so an opportunity that could only
                    # ever be profitable via maker orders still gets through
                    # to the Maker/Taker Strategy Engine below. The taker
                    # break-even (the conservative, certain-fill reference)
                    # is still what gets reported and used for classification.
                    taker_break_even = compute_break_even(
                        self.fee_engine, [(buy_exchange, MarketType.SPOT, False), (sell_exchange, MarketType.SPOT, False)]
                    )
                    maker_break_even = compute_break_even(
                        self.fee_engine, [(buy_exchange, MarketType.SPOT, True), (sell_exchange, MarketType.SPOT, True)]
                    )
                    if gross_spread_pct < maker_break_even.total_pct:
                        continue

                    opp = self._price(
                        symbol,
                        buy_exchange,
                        buy_quote,
                        sell_exchange,
                        sell_quote,
                        gross_spread_pct,
                        taker_break_even.total_pct,
                        data_quality.market_data_age_seconds,
                    )
                    if opp is not None:
                        opportunities.append(opp)
        return opportunities

    def _price(
        self, symbol, buy_exchange, buy_quote, sell_exchange, sell_quote, gross_spread_pct, break_even_pct, market_data_age_seconds
    ) -> Opportunity | None:
        # Real multi-level depth where a local order book is available and
        # fresh — all 3 priority exchanges as of the Opportunity Expansion
        # spec, Step 2 (2026-08-21) — everywhere else, still the single
        # top-of-book level collectors have always carried.
        now = time.time()
        ask_levels = _resolve_ask_levels(self.store, buy_exchange, symbol, buy_quote, now)
        bid_levels = _resolve_bid_levels(self.store, sell_exchange, symbol, sell_quote, now)

        # Cheap feasibility check at the standard intended size first — no
        # point walking the full depth-adjusted-edge curve below if there's
        # no liquidity here at all.
        buy_fill = simulate_vwap(ask_levels, self.capital_usd)
        sell_fill = simulate_vwap(bid_levels, self.capital_usd)
        if buy_fill.filled_usd <= 0 or sell_fill.filled_usd <= 0:
            return None

        # Opportunity Expansion spec, Step 2 (user directive, 2026-08-21) —
        # a theoretical top-of-book edge must never be treated as
        # executable just because it's positive at one fixed size (spec's
        # own example: +0.10% with only $50 of depth against a $5,000
        # intent isn't a real opportunity). Walk real depth on both legs
        # across a spread of capital sizes and find the size that actually
        # maximizes real dollar profit — not the size a fixed default
        # happened to name, and not "however much capital happens to be
        # available" either.
        edge = compute_depth_adjusted_edge(
            buy_exchange, sell_exchange, ask_levels, bid_levels, gross_spread_pct, self.fee_engine, self.capital_usd
        )
        if edge.optimal_capital_usd is None:
            # Positive at the naive fixed size's approximation, but never
            # actually profitable once real depth is walked at any tested
            # size — not a real opportunity. Same rejection semantics as
            # "no liquidity at all" above, never a loosened threshold.
            return None

        capital = edge.optimal_capital_usd
        buy_fill = simulate_vwap(ask_levels, capital)
        sell_fill = simulate_vwap(bid_levels, capital)
        filled_usd = min(buy_fill.filled_usd, sell_fill.filled_usd)
        quantity = (filled_usd / buy_fill.average_price) if buy_fill.average_price else 0.0
        # net_profit/net_spread_pct come straight from `edge`'s own optimal
        # tier (identical formula, already computed) rather than being
        # re-derived here — one source of truth, no risk of the two drifting.
        net_profit = edge.optimal_net_profit_usd
        net_spread_pct = edge.realistic_executable_edge_pct

        # Maker/Taker Strategy Engine: on top of the certain-fill taker/taker
        # numbers above, work out whether resting a maker order on either
        # leg beats it in expectation, given the fill risk.
        best_execution = simulate_best_execution(buy_exchange, sell_exchange, buy_quote, sell_quote, filled_usd, self.fee_engine, self.store)

        return Opportunity(
            strategy=self.strategy,
            symbol=symbol,
            legs=[
                {"exchange": buy_exchange, "side": "buy", "market": "spot", "price": buy_fill.average_price, "quantity": quantity},
                {"exchange": sell_exchange, "side": "sell", "market": "spot", "price": sell_fill.average_price, "quantity": quantity},
            ],
            gross_spread_pct=gross_spread_pct,
            net_spread_pct=net_spread_pct,
            break_even_pct=break_even_pct,
            capital_usd=filled_usd,
            expected_profit_usd=net_profit,
            execution_mode=best_execution.mode,
            execution_fill_probability=best_execution.fill_probability,
            market_data_age_seconds=market_data_age_seconds,
            holding_period_seconds=NOMINAL_FAST_HOLDING_SECONDS,
            theoretical_edge_pct=edge.theoretical_edge_pct,
            depth_adjusted_edge_pct=edge.depth_adjusted_edge_pct,
            realistic_executable_edge_pct=edge.realistic_executable_edge_pct,
            optimal_capital_usd=edge.optimal_capital_usd,
            max_profitable_capital_usd=edge.max_profitable_capital_usd,
        )
