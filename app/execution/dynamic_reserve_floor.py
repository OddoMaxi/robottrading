"""DYNAMIC RESERVE FLOOR MODEL (user directive, 2026-08-25, "MISSION --
V5 CAPITAL EFFICIENCY + OKX VALIDATION"). Replaces the single fixed
"25 USDT everywhere" figure (app.execution.capital_rebalancer.
compute_reserve_floor, left UNCHANGED and still the only floor actually
used in production -- this module is explicitly NOT wired into any real
decision path yet, per the user's own "Ne modifie pas encore les floors
de production. Construis d'abord le modele et fais un replay/shadow.")
with a floor that reflects what an exchange's OWN real usage pattern
and real constraints actually require.

The model has three components, combined by simple max/min composition
so each one's role stays legible rather than folded into one opaque
score:

1. STRUCTURAL MINIMUM -- an exchange must always be able to fund at
   least min_floor_multiple_of_min_notional (default 2) minimum-sized
   orders, or its floor is meaningless (it would already be unusable
   before any "reserve" concept even applies). Derived from the
   exchange's real min_notional, never a bare number.

2. OPERATING BUFFER -- safety_multiplier * max_notional_per_leg *
   IMBALANCE_FACTOR, where IMBALANCE_FACTOR = recent_buy_count /
   max(1, recent_sell_count), clamped to [1.0, imbalance_factor_cap].
   An exchange that is mostly a BUY source (capital draining, never
   replenished by sells) needs more headroom than one that is mostly a
   SELL target (capital naturally accumulating there); an exchange with
   no recent buy activity at all still gets the floor of 1.0x, since a
   direction reversal is always possible.

3. EQUAL-SHARE CAP -- no single exchange's floor may exceed
   max_floor_share_of_total_capital of an equal per-exchange split of
   total capital across every active exchange. This is what actually
   answers "combien de capital doit rester disponible" as a system-wide
   question, not a per-exchange one in isolation: with 3 active
   exchanges and a 60% share cap, no floor can exceed 0.6 * (total/3) --
   preventing a single exchange's operating buffer from starving the
   other two of the deployable capital they need too.

Real fees and depth are DELIBERATELY not separate terms in this
formula: fees are already reflected in min_notional (an exchange's own
minimum tradable size already prices in what a viable trade must clear)
and depth is a PER-OPPORTUNITY concern handled by compute_dual_leg_quote/
evaluate_arbitrage_true_economics at trade time, not a STANDING capital
buffer concern -- conflating the two would make this floor swing on
every scan's market conditions instead of representing a stable
capital-adequacy policy."""

from dataclasses import dataclass

DEFAULT_SAFETY_MULTIPLIER = 2.5  # matches capital_rebalancer.compute_reserve_floor's own default, for comparability
DEFAULT_MIN_FLOOR_MULTIPLE_OF_MIN_NOTIONAL = 2.0
DEFAULT_MAX_FLOOR_SHARE_OF_TOTAL_CAPITAL = 0.6
DEFAULT_IMBALANCE_FACTOR_CAP = 5.0


@dataclass(slots=True, frozen=True)
class DynamicReserveFloorResult:
    exchange: str
    floor_usdt: float
    structural_minimum_usdt: float
    operating_buffer_usdt: float
    equal_share_cap_usdt: float
    imbalance_factor: float
    binding_component: str  # "STRUCTURAL_MINIMUM" | "OPERATING_BUFFER" | "EQUAL_SHARE_CAP"


def compute_dynamic_reserve_floor(
    *,
    exchange: str,
    max_notional_per_leg_usdt: float,
    min_notional_usdt: float,
    recent_buy_count: int,
    recent_sell_count: int,
    total_capital_usdt: float,
    num_active_exchanges: int,
    safety_multiplier: float = DEFAULT_SAFETY_MULTIPLIER,
    min_floor_multiple_of_min_notional: float = DEFAULT_MIN_FLOOR_MULTIPLE_OF_MIN_NOTIONAL,
    max_floor_share_of_total_capital: float = DEFAULT_MAX_FLOOR_SHARE_OF_TOTAL_CAPITAL,
    imbalance_factor_cap: float = DEFAULT_IMBALANCE_FACTOR_CAP,
) -> DynamicReserveFloorResult:
    """Pure. All inputs are real, caller-supplied figures -- this
    function invents nothing (no clock reads, no history lookups; the
    caller derives recent_buy_count/recent_sell_count from real shadow/
    trade history and total_capital_usdt from real balances)."""
    structural_minimum = min_floor_multiple_of_min_notional * min_notional_usdt

    imbalance_factor = recent_buy_count / max(1, recent_sell_count)
    imbalance_factor = max(1.0, min(imbalance_factor_cap, imbalance_factor))
    operating_buffer = safety_multiplier * max_notional_per_leg_usdt * imbalance_factor

    equal_share_cap = (total_capital_usdt / max(1, num_active_exchanges)) * max_floor_share_of_total_capital

    # Never below the structural minimum (an exchange must always be
    # able to fund its own smallest possible order), never above the
    # equal-share cap (no exchange starves the others) -- the operating
    # buffer sets where it lands IN BETWEEN those two bounds.
    uncapped = max(structural_minimum, operating_buffer)
    floor = min(uncapped, max(equal_share_cap, structural_minimum))

    if floor == structural_minimum and structural_minimum >= operating_buffer:
        binding = "STRUCTURAL_MINIMUM"
    elif floor < operating_buffer:
        binding = "EQUAL_SHARE_CAP"
    else:
        binding = "OPERATING_BUFFER"

    return DynamicReserveFloorResult(
        exchange=exchange, floor_usdt=round(floor, 6), structural_minimum_usdt=round(structural_minimum, 6),
        operating_buffer_usdt=round(operating_buffer, 6), equal_share_cap_usdt=round(equal_share_cap, 6),
        imbalance_factor=round(imbalance_factor, 4), binding_component=binding,
    )
