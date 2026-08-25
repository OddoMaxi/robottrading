"""CAPITAL SCALING OBSERVER (user directive, 2026-08-25, AUTONOMOUS 24/7
operation, item 9). For a given candidate (symbol, buy_exchange,
sell_exchange), reuses ONE real market snapshot per leg -- the exact
same LegSnapshot/compute_dual_leg_quote machinery this project already
relies on for REAL sizing decisions (app.execution.dual_leg_quote) -- to
compute what the SAME real depth/fees would do at each of several
hypothetical notional tiers (10/20/50/100/250/500/1000 USDT).

Observation-only: this module places no order and is never consulted by
any trade-sizing decision. The real trading cap
(settings.max_notional_per_leg_usdt) is never derived from, or adjusted
by, anything computed here -- these are two structurally independent
values, and nothing in this codebase wires one to the other.

Reusing the same leg snapshots across every tier (rather than
re-fetching fresh market data per tier) gives a fair, same-point-in-time
comparison -- comparing a 10 USDT quote fetched at T0 against a 1000
USDT quote fetched two seconds later at T0+2s would conflate real market
movement with the effect of size itself."""

import uuid
from dataclasses import dataclass

from app.execution.dual_leg_quote import LegSnapshot, compute_dual_leg_quote

OBSERVATION_TIERS_USDT: tuple[float, ...] = (10.0, 20.0, 50.0, 100.0, 250.0, 500.0, 1000.0)


@dataclass(slots=True, frozen=True)
class ScalingObservation:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    tier_usdt: float
    executable: bool
    executable_qty: float
    depth_sufficient: bool  # real book depth could absorb the FULL size requested at this tier (independent of whether it was still net-profitable)
    buy_slippage_pct: float
    sell_slippage_pct: float
    total_fees_usd: float
    net_profit_usd: float
    net_return_bps: float
    reason: str | None


def observe_at_tiers(
    *, symbol: str, buy_leg: LegSnapshot, sell_leg: LegSnapshot, opportunity_id: uuid.UUID,
    tiers_usdt: tuple[float, ...] = OBSERVATION_TIERS_USDT,
) -> list[ScalingObservation]:
    """Pure. `opportunity_id` is audit-trail metadata only on the
    underlying DualLegQuote (it does not affect any of this function's
    own computed values) -- generated once by the caller, matching this
    project's standing rule that pure functions never call uuid4()/
    time.time() themselves."""
    observations = []
    for tier in tiers_usdt:
        quote = compute_dual_leg_quote(
            opportunity_id=opportunity_id, symbol=symbol, buy_leg=buy_leg, sell_leg=sell_leg,
            master_requested_size_usd=tier, micro_live_cap_usdt=tier,
        )
        # compute_dual_leg_quote never actually caps executable_qty by
        # real depth (it is always requested_qty rounded to step) --
        # insufficient visible depth is instead signaled by pinning
        # slippage to a 100% sentinel (see _vwap_for_target_qty's
        # caller in dual_leg_quote.py). Reusing that existing signal
        # directly is correct; independently re-deriving "was the
        # depth enough" from executable_qty is not, since that field
        # does not carry the information at all.
        depth_sufficient = quote.buy_slippage_pct < 100.0 and quote.sell_slippage_pct < 100.0
        observations.append(ScalingObservation(
            symbol=symbol, buy_exchange=buy_leg.exchange, sell_exchange=sell_leg.exchange, tier_usdt=tier,
            executable=quote.executable, executable_qty=quote.executable_qty, depth_sufficient=depth_sufficient,
            buy_slippage_pct=quote.buy_slippage_pct, sell_slippage_pct=quote.sell_slippage_pct,
            total_fees_usd=quote.buy_fee_usd + quote.sell_fee_usd, net_profit_usd=quote.net_profit_usd,
            net_return_bps=quote.net_return_bps, reason=quote.reason,
        ))
    return observations
