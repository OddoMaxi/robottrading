"""Break-Even Spread Engine — the minimum gross spread needed to clear all
real costs, computed upfront so an opportunity can be classified
NOT_PROFITABLE before running the rest of the pipeline.

Break-even = trading fees on every leg (computed exactly, per exchange and
maker/taker mode) + a slippage buffer + a rebalancing allowance + a safety
margin. The last three are configurable assumptions — the Liquidity/
Slippage engines price *actual* slippage per opportunity once quotes are in
hand; this is a cheap upfront floor, not a replacement for that.
"""

from dataclasses import dataclass

from app.analytics.fees import FeeEngine
from app.config.constants import (
    DEFAULT_REBALANCING_BUFFER_PCT,
    DEFAULT_SAFETY_MARGIN_PCT,
    DEFAULT_SLIPPAGE_BUFFER_PCT,
    MarketType,
)


@dataclass(slots=True)
class BreakEvenBreakdown:
    trading_fees_pct: float
    slippage_buffer_pct: float
    rebalancing_pct: float
    safety_margin_pct: float

    @property
    def total_pct(self) -> float:
        return self.trading_fees_pct + self.slippage_buffer_pct + self.rebalancing_pct + self.safety_margin_pct


def compute_break_even(
    fee_engine: FeeEngine,
    legs: list[tuple[str, MarketType, bool]],  # (exchange, market, is_maker) — one entry per trade leg
    slippage_buffer_pct: float = DEFAULT_SLIPPAGE_BUFFER_PCT,
    rebalancing_pct: float = DEFAULT_REBALANCING_BUFFER_PCT,
    safety_margin_pct: float = DEFAULT_SAFETY_MARGIN_PCT,
) -> BreakEvenBreakdown:
    # trading_fee(..., notional_usd=100, ...) returns 100 * rate, i.e. the fee
    # rate expressed directly as a percentage — fee % doesn't depend on the
    # notional size, so 100 is just a convenient unit, not an assumption.
    trading_fees_pct = sum(
        fee_engine.trading_fee(exchange, market, 100.0, is_maker=is_maker) for exchange, market, is_maker in legs
    )
    return BreakEvenBreakdown(
        trading_fees_pct=trading_fees_pct,
        slippage_buffer_pct=slippage_buffer_pct,
        rebalancing_pct=rebalancing_pct,
        safety_margin_pct=safety_margin_pct,
    )
