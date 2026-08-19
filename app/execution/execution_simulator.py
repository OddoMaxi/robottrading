"""Glue between live market data and the Maker/Taker Strategy Engine — called
from the detection engines to find the best execution mode for a 2-leg
opportunity, informationally, alongside the existing certain-fill (taker)
net profit calculation.
"""

from app.analytics.fees import FeeEngine
from app.execution.fill_probability import estimate_maker_fill_probability
from app.execution.maker_taker import ExecutionModeResult, best_execution_mode, evaluate_execution_modes
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore


def simulate_best_execution(
    buy_exchange: str,
    sell_exchange: str,
    buy_quote: NormalizedQuote,
    sell_quote: NormalizedQuote,
    capital_usd: float,
    fee_engine: FeeEngine,
    store: MarketDataStore,
) -> ExecutionModeResult:
    buy_mid = (buy_quote.bid + buy_quote.ask) / 2
    buy_spread_pct = (buy_quote.ask - buy_quote.bid) / buy_mid * 100 if buy_mid > 0 else 0.0
    sell_mid = (sell_quote.bid + sell_quote.ask) / 2
    sell_spread_pct = (sell_quote.ask - sell_quote.bid) / sell_mid * 100 if sell_mid > 0 else 0.0

    buy_touch_usd = buy_quote.bid * buy_quote.bid_quantity
    sell_touch_usd = sell_quote.ask * sell_quote.ask_quantity

    buy_volatility = store.recent_volatility_pct(buy_exchange, buy_quote.symbol)
    sell_volatility = store.recent_volatility_pct(sell_exchange, sell_quote.symbol)

    buy_fill_probability, _ = estimate_maker_fill_probability(buy_spread_pct, buy_touch_usd, capital_usd, buy_volatility)
    sell_fill_probability, _ = estimate_maker_fill_probability(sell_spread_pct, sell_touch_usd, capital_usd, sell_volatility)

    results = evaluate_execution_modes(
        buy_exchange,
        sell_exchange,
        buy_quote.bid,
        buy_quote.ask,
        sell_quote.bid,
        sell_quote.ask,
        capital_usd,
        fee_engine,
        buy_fill_probability,
        sell_fill_probability,
    )
    return best_execution_mode(results)
