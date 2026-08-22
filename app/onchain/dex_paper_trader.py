"""DEX Paper Trading Engine (Multi-Market Opportunity Engine, V5.5
continuation, user directive, 2026-08-22).

A separate, isolated simulation layer from app.simulation.paper_trader —
DEX opportunities are NEVER handed to that CEX-specific engine or to any
CEX VirtualPortfolio (spec section 39: DEX additions must not alter
historical CEX accounting; spec section 1: a bug here must never affect
CEX). This module gives DEX its own capital pool and its own
execution-outcome simulation — entirely separate money, entirely separate
ledger, same isolation guarantee proven for detection now extended to
execution.

Every field the detection-time Opportunity already carries (realistic
edge, capital, fill probability, market_data_age) already reflects real
prices, depth, price impact, slippage, swap fees, gas, and MEV buffer —
see app.onchain.cross_dex_arbitrage/multihop_arbitrage/atomic_arbitrage.
What THIS module adds is what can only be known at attempt time: real
capital availability against a shared pool (no double-spending — spec
item 6), a genuine timestamped pipeline (spec item 8), and, most
importantly, REVALIDATION with a realistic price-drift model over the
REAL chain-specific inclusion latency (spec item 7) — "a profitable
opportunity at detection can vanish milliseconds later" is modeled
honestly here as a documented, conservative random walk over the actual
computed broadcast+mempool+inclusion window
(app.onchain.execution_model.ChainExecutionModel), not a live re-fetch
(GeckoTerminal's free tier can't sustain a re-poll per attempt — confirmed
live, 429s under much lighter load than that would require) and not a
fabricated "it always still works" assumption either.

flash_loan_research opportunities are deliberately excluded from this
pool — spec section 35: flash loan research must never reduce available
OWN capital, and this module's pool represents real own capital shadow
trading.

REALITY AUDIT FIX (2026-08-22, user directive, section 7): the pool used
to reserve() and resolve() synchronously within one Python call, so no
real wall-clock time ever separated a reservation from its release —
capital was always back to "fully available" before the very next
opportunity in the same scan cycle was attempted, even though the
persisted validation_to_execution_ms showed a multi-second execution
window. That meant genuine simultaneous capital contention (two
opportunities detected in the same cycle both wanting the same dollars)
was never actually exercised, and was directly confirmed live: an
"atomic" and its "dex_triangular" sibling both reserved the full $5,000
pool for the same 19-second window. The pool now tracks time-windowed
reservations keyed by opportunity id: capital reserved at attempt time
stays locked until execution_complete_timestamp (the real, chain-specific
inclusion latency), so a second opportunity attempted moments later in
the same cycle correctly sees reduced available capital and can generate
NO_CAPITAL_AVAILABLE from contention, not just from an already-exhausted
pool. P&L is booked to realized_pnl_usd immediately once an outcome is
known (this is deterministic retrospective simulation, not live async
waiting) — only the principal stays locked for the window.
"""

import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum

from app.onchain.execution_model import build_execution_model
from app.opportunity.models import Opportunity

DEX_ATTEMPTABLE_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic")

# Documented, conservative random-walk assumption for how much a DEX price
# can drift, in percent, over one second of real inclusion latency — NOT a
# measurement (no historical block-by-block replay exists yet to calibrate
# it against, same caveat as app.onchain.execution_model's
# expected_opportunity_lifetime_seconds). Scaled by sqrt(latency_seconds),
# the standard random-walk scaling, so Ethereum's much longer inclusion
# window gets proportionally more drift variance than Solana's.
PRICE_DRIFT_STD_PCT_PER_SQRT_SECOND = 0.03

# Time to decide whether to submit, after a fresh detection — real but tiny
# next to on-chain inclusion latency; a placeholder for actual
# decision/signing overhead, not fabricated precision.
DECISION_LATENCY_SECONDS = 0.05


class DexTradeStatus(StrEnum):
    FILLED = "dex_filled"  # simulated success — profit booked
    EDGE_DISAPPEARED = "dex_edge_disappeared"  # revalidation caught it before the attempt — spec item 7's own rule: never counted as an execution failure
    FAILED = "dex_failed"  # attempted, but the fill/inclusion probability roll failed — gas spent, no profit
    NO_CAPITAL_AVAILABLE = "dex_no_capital_available"
    NOT_PROFITABLE_AT_SIZE = "dex_not_profitable_at_size"  # reality audit section 4: edge survived revalidation, but re-sizing against currently-available capital found no size with net_profit_usd > 0 — never executed, distinct from EDGE_DISAPPEARED (the edge itself is real; only the achievable size at this moment isn't)


@dataclass(slots=True)
class _Reservation:
    amount: float
    release_at: float


@dataclass(slots=True)
class DexCapitalPool:
    """Time-windowed capital reservations — spec item 6 and the reality
    audit's section 7 fix. total_capital_usd is fixed (this is a
    shadow/paper pool, nothing ever deposits into or withdraws from it
    beyond what trades themselves realize as profit or loss). A
    reservation locked at attempt time genuinely stays locked until
    `release_at` (real inclusion latency), so opportunities attempted
    moments apart in the same scan cycle correctly compete for the same
    dollars instead of each seeing the full pool."""

    total_capital_usd: float
    realized_pnl_usd: float = 0.0
    _reservations: dict[uuid.UUID, _Reservation] = field(default_factory=dict)

    def _prune_expired(self, now: float) -> None:
        expired = [key for key, res in self._reservations.items() if res.release_at <= now]
        for key in expired:
            del self._reservations[key]

    def locked_capital_usd(self, now: float) -> float:
        self._prune_expired(now)
        return sum(res.amount for res in self._reservations.values())

    def available_capital_usd(self, now: float) -> float:
        return self.total_capital_usd + self.realized_pnl_usd - self.locked_capital_usd(now)

    def reserve(self, reservation_id: uuid.UUID, amount: float, now: float, release_at: float) -> bool:
        if amount <= 0 or amount > self.available_capital_usd(now) + 1e-9:
            return False
        self._reservations[reservation_id] = _Reservation(amount=amount, release_at=release_at)
        return True

    def resolve_pnl(self, net_profit_usd: float) -> None:
        """The outcome of an in-flight (still time-locked) reservation is
        now known — books P&L immediately (deterministic retrospective
        simulation, not live async waiting). The reservation's principal
        stays locked until its own release_at; this does not touch it."""
        self.realized_pnl_usd += net_profit_usd


@dataclass(slots=True)
class DexTradeAttempt:
    opportunity_id: uuid.UUID
    strategy: str
    symbol: str
    chain: str
    status: DexTradeStatus
    capital_usd: float
    net_profit_usd: float
    revalidated_net_pct: float | None
    detection_timestamp: float
    validation_timestamp: float
    execution_attempt_timestamp: float
    execution_complete_timestamp: float

    @property
    def detection_to_validation_ms(self) -> float:
        return (self.validation_timestamp - self.detection_timestamp) * 1000

    @property
    def validation_to_execution_ms(self) -> float:
        return (self.execution_complete_timestamp - self.execution_attempt_timestamp) * 1000

    @property
    def total_execution_ms(self) -> float:
        return (self.execution_complete_timestamp - self.detection_timestamp) * 1000


def revalidate_edge(opp: Opportunity, chain: str, rng) -> tuple[bool, float]:
    """Recomputes the edge with a simulated realistic drift over the REAL,
    chain-specific inclusion latency — same documented-assumption
    philosophy as app.execution.latency_engine.revalidate_after_latency
    uses for CEX (a fixed latency profile there), applied here to DEX's
    own real, chain-differentiated latency number instead."""
    inclusion = build_execution_model(chain).estimate_inclusion()
    drift_std_pct = PRICE_DRIFT_STD_PCT_PER_SQRT_SECOND * (inclusion.total_seconds**0.5)
    drift_pct = rng.gauss(0.0, drift_std_pct)
    base_edge_pct = opp.realistic_executable_edge_pct if opp.realistic_executable_edge_pct is not None else 0.0
    revalidated_net_pct = base_edge_pct + drift_pct
    return revalidated_net_pct > 0, revalidated_net_pct


def resize_at_attempt(opp: Opportunity, drift_pct: float, available_capital_usd: float, gas_cost_usd: float, slippage_buffer_pct: float | None = None):
    """Reality audit section 4: re-runs the SAME tiered capital-sizing
    sweep used at detection time (app.onchain.cross_dex_arbitrage's own
    Smart Position Sizing), but now against capital actually available in
    the pool at attempt time, and against the REVALIDATED edge rather than
    the stale detection-time one. `drift_pct` (the change revalidate_edge
    found, not the absolute %) is applied to the sell leg's price so
    size-dependent AMM price-impact still recomputes correctly instead of
    naively rescaling the final percentage — the same reconstruction
    app.reporting.dex_replay uses to independently recompute detection
    math from the snapshotted legs. Only implemented for the plain 2-leg
    dex_cross shape (same scope limit as the replay module); returns None
    for every other strategy, so callers fall back to a simple clip — a
    documented, honest gap, not silently papered over.

    Returns the DexTierResult picked as the new optimal size, or None if
    no tested size (capped at available_capital_usd) is profitable."""
    if opp.strategy != "dex_cross" or available_capital_usd <= 0:
        return None
    legs = opp.legs or []
    if len(legs) != 2 or not all("price" in leg and "tvl_usd" in leg and "fee_pct" in leg and "pool_id" in leg and "chain" in leg and "exchange" in leg for leg in legs):
        return None

    from app.onchain.constants import DEX_CAPITAL_TEST_TIERS_USD
    from app.onchain.cross_dex_arbitrage import compute_dex_depth_adjusted_edge
    from app.onchain.models import DexPool

    buy_leg, sell_leg = legs[0], legs[1]
    buy_pool = DexPool(
        chain=buy_leg["chain"], dex=buy_leg["exchange"], pool_id=buy_leg["pool_id"],
        token0_symbol="A", token1_symbol="B", price=buy_leg["price"], tvl_usd=buy_leg["tvl_usd"],
        volume_24h_usd=0.0, fee_pct=buy_leg["fee_pct"], pool_created_at=None, last_update=0.0,
    )
    sell_price_adjusted = sell_leg["price"] * (1 + drift_pct / 100)
    sell_pool = DexPool(
        chain=sell_leg["chain"], dex=sell_leg["exchange"], pool_id=sell_leg["pool_id"],
        token0_symbol="A", token1_symbol="B", price=sell_price_adjusted, tvl_usd=sell_leg["tvl_usd"],
        volume_24h_usd=0.0, fee_pct=sell_leg["fee_pct"], pool_created_at=None, last_update=0.0,
    )

    tiers_capped_usd = sorted({size for size in DEX_CAPITAL_TEST_TIERS_USD if size <= available_capital_usd} | {available_capital_usd})
    kwargs = {} if slippage_buffer_pct is None else {"slippage_buffer_pct": slippage_buffer_pct}
    edge = compute_dex_depth_adjusted_edge(
        buy_pool, sell_pool, buy_pool.price, sell_price_adjusted, gas_cost_usd,
        theoretical_edge_pct=0.0, test_tiers_usd=tiers_capped_usd, **kwargs,
    )
    if edge.optimal_capital_usd is None or edge.optimal_net_profit_usd is None or edge.optimal_net_profit_usd <= 0:
        return None
    return next(t for t in edge.tiers if t.capital_usd == edge.optimal_capital_usd)


def attempt_dex_trade(opp: Opportunity, pool: DexCapitalPool, gas_cost_usd: float, rng, now: float | None = None) -> DexTradeAttempt:
    """The full pipeline: revalidation (spec item 7) -> re-sizing against
    the revalidated edge and currently-available capital (spec item 4) ->
    never execute if the resulting net_profit_usd <= 0 -> reserve capital
    for the real inclusion window (spec item 7's own window, reality audit
    section 7) -> fill/fail roll -> resolve. Capital is only ever reserved
    once we already know the trade is worth attempting — deciding whether
    an opportunity is still real doesn't cost capital, only broadcasting a
    transaction does."""
    now = now if now is not None else time.time()
    chain = opp.legs[0].get("chain") if opp.legs else None
    detection_timestamp = opp.detected_at or now
    validation_timestamp = detection_timestamp + DECISION_LATENCY_SECONDS

    def _result(status: DexTradeStatus, capital_usd: float, net_profit_usd: float, revalidated_net_pct: float | None, exec_attempt: float, exec_complete: float) -> DexTradeAttempt:
        return DexTradeAttempt(
            opportunity_id=opp.id,
            strategy=opp.strategy,
            symbol=opp.symbol,
            chain=chain or "unknown",
            status=status,
            capital_usd=capital_usd,
            net_profit_usd=net_profit_usd,
            revalidated_net_pct=revalidated_net_pct,
            detection_timestamp=detection_timestamp,
            validation_timestamp=validation_timestamp,
            execution_attempt_timestamp=exec_attempt,
            execution_complete_timestamp=exec_complete,
        )

    if chain is None:
        return _result(DexTradeStatus.FAILED, 0.0, 0.0, None, validation_timestamp, validation_timestamp)

    still_valid, revalidated_net_pct = revalidate_edge(opp, chain, rng)
    if not still_valid:
        return _result(DexTradeStatus.EDGE_DISAPPEARED, 0.0, 0.0, revalidated_net_pct, validation_timestamp, validation_timestamp)

    available_now = pool.available_capital_usd(now)
    drift_pct = revalidated_net_pct - (opp.realistic_executable_edge_pct if opp.realistic_executable_edge_pct is not None else 0.0)
    resized = resize_at_attempt(opp, drift_pct, available_now, gas_cost_usd)
    if resized is not None:
        capital_usd = resized.capital_usd
        projected_net_profit_usd = resized.net_profit_usd
    else:
        capital_usd = min(opp.capital_usd or 0.0, available_now)
        projected_net_profit_usd = capital_usd * (revalidated_net_pct / 100) if capital_usd > 0 else 0.0

    if capital_usd <= 0:
        return _result(DexTradeStatus.NO_CAPITAL_AVAILABLE, 0.0, 0.0, revalidated_net_pct, validation_timestamp, validation_timestamp)

    if projected_net_profit_usd <= 0:
        # Spec item 4's explicit rule: never execute if net_profit(size) <= 0
        # — the edge itself survived revalidation, but no achievable size
        # right now is actually worth it.
        return _result(DexTradeStatus.NOT_PROFITABLE_AT_SIZE, 0.0, 0.0, revalidated_net_pct, validation_timestamp, validation_timestamp)

    execution_attempt_timestamp = validation_timestamp  # transaction broadcast happens right after validation
    inclusion = build_execution_model(chain).estimate_inclusion()
    execution_complete_timestamp = execution_attempt_timestamp + inclusion.total_seconds

    if not pool.reserve(opp.id, capital_usd, now, execution_complete_timestamp):
        return _result(DexTradeStatus.NO_CAPITAL_AVAILABLE, 0.0, 0.0, revalidated_net_pct, validation_timestamp, validation_timestamp)

    fill_probability = opp.execution_fill_probability if opp.execution_fill_probability is not None else 1.0
    if rng.random() > fill_probability:
        # Attempted, inclusion/fill roll failed — gas is spent regardless
        # of outcome (broadcasting the transaction is what costs gas, not
        # its success), no profit. Never a principal loss beyond gas: see
        # module docstring — a more pessimistic non-atomic
        # leg-fails-after-the-other-fills model is a documented future
        # refinement, not built tonight.
        net_profit_usd = -gas_cost_usd
        pool.resolve_pnl(net_profit_usd)
        return _result(DexTradeStatus.FAILED, capital_usd, net_profit_usd, revalidated_net_pct, execution_attempt_timestamp, execution_complete_timestamp)

    # gas is NOT subtracted again here — projected_net_profit_usd already
    # nets gas out (either via resize_at_attempt's evaluate_dex_capital_tier,
    # or via revalidated_net_pct which derives from
    # opp.realistic_executable_edge_pct, itself already gas-netted at
    # detection time) — subtracting it a second time here would
    # double-count the exact same cost.
    net_profit_usd = projected_net_profit_usd
    pool.resolve_pnl(net_profit_usd)
    return _result(DexTradeStatus.FILLED, capital_usd, net_profit_usd, revalidated_net_pct, execution_attempt_timestamp, execution_complete_timestamp)
