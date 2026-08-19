"""Liquidity Engine (section 12) — never trust the top-of-book price alone."""

from dataclasses import dataclass

from app.config.constants import LIQUIDITY_TEST_AMOUNTS_USD
from app.market_data.orderbook import OrderBookLevel, VwapResult, simulate_vwap


@dataclass(slots=True)
class LiquidityProfile:
    exchange: str
    symbol: str
    side: str  # "bid" or "ask"
    results: dict[float, VwapResult]  # keyed by test amount in USD


class LiquidityEngine:
    def __init__(self, test_amounts_usd: list[float] = LIQUIDITY_TEST_AMOUNTS_USD) -> None:
        self.test_amounts_usd = test_amounts_usd

    def profile(self, exchange: str, symbol: str, side: str, levels: list[OrderBookLevel]) -> LiquidityProfile:
        results = {amount: simulate_vwap(levels, amount) for amount in self.test_amounts_usd}
        return LiquidityProfile(exchange=exchange, symbol=symbol, side=side, results=results)
