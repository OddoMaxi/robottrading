"""Slippage Engine (section 13) — displayed price vs. simulated execution price."""

from dataclasses import dataclass


@dataclass(slots=True)
class SlippageResult:
    displayed_price: float
    execution_price: float
    slippage_pct: float
    slippage_usd: float


def compute_slippage(displayed_price: float, execution_price: float, quantity: float) -> SlippageResult:
    slippage_pct = (execution_price - displayed_price) / displayed_price * 100
    slippage_usd = (execution_price - displayed_price) * quantity
    return SlippageResult(
        displayed_price=displayed_price,
        execution_price=execution_price,
        slippage_pct=slippage_pct,
        slippage_usd=slippage_usd,
    )
