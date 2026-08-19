"""Order book depth model and VWAP execution simulation (feeds section 12/13)."""

from dataclasses import dataclass


@dataclass(slots=True)
class OrderBookLevel:
    price: float
    quantity: float


@dataclass(slots=True)
class OrderBook:
    exchange: str
    symbol: str
    bids: list[OrderBookLevel]  # sorted best (highest) first
    asks: list[OrderBookLevel]  # sorted best (lowest) first
    timestamp: float


@dataclass(slots=True)
class VwapResult:
    target_usd: float
    filled_usd: float
    average_price: float
    worst_price: float
    fully_filled: bool


def simulate_vwap(levels: list[OrderBookLevel], target_usd: float) -> VwapResult:
    """Walk the book and compute the volume-weighted average price to fill target_usd.

    Used by the Liquidity Engine (section 12) to test each of the fixed
    capital tiers, and by the Slippage Engine (section 13) to compare against
    the displayed top-of-book price.
    """
    remaining_usd = target_usd
    filled_usd = 0.0
    filled_quantity = 0.0
    worst_price = levels[0].price if levels else 0.0

    for level in levels:
        level_usd = level.price * level.quantity
        take_usd = min(remaining_usd, level_usd)
        take_quantity = take_usd / level.price

        filled_usd += take_usd
        filled_quantity += take_quantity
        worst_price = level.price
        remaining_usd -= take_usd

        if remaining_usd <= 0:
            break

    average_price = (filled_usd / filled_quantity) if filled_quantity else 0.0
    return VwapResult(
        target_usd=target_usd,
        filled_usd=filled_usd,
        average_price=average_price,
        worst_price=worst_price,
        fully_filled=remaining_usd <= 1e-9,
    )
