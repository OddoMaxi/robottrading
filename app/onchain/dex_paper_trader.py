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


@dataclass(slots=True)
class DexCapitalPool:
    """available/reserved/deployed, always consistent — spec item 6's own
    field names. total_capital_usd is fixed (this is a shadow/paper pool,
    nothing ever deposits into or withdraws from it beyond what trades
    themselves realize as profit or loss)."""

    total_capital_usd: float
    reserved_capital_usd: float = 0.0  # locked for an in-flight attempt, outcome not yet resolved
    deployed_capital_usd: float = 0.0  # currently committed to a "filled" position (near-zero most of the time — DEX resolution is near-instant)
    realized_pnl_usd: float = 0.0

    @property
    def available_capital_usd(self) -> float:
        return self.total_capital_usd + self.realized_pnl_usd - self.reserved_capital_usd - self.deployed_capital_usd

    def reserve(self, amount: float) -> bool:
        if amount <= 0 or amount > self.available_capital_usd + 1e-9:
            return False
        self.reserved_capital_usd += amount
        return True

    def release_reservation(self, amount: float) -> None:
        self.reserved_capital_usd = max(0.0, self.reserved_capital_usd - amount)

    def resolve(self, amount: float, net_profit_usd: float) -> None:
        """A reserved amount's outcome is now known — moves it out of
        reserved, books the P&L, and (since every DEX trade here resolves
        essentially instantly — a swap bundle either lands in one block or
        it doesn't) never actually sits in deployed_capital_usd for a
        caller to observe mid-flight. deployed_capital_usd exists for the
        invariant's sake and for a future non-instant execution model, not
        because today's trades linger there."""
        self.reserved_capital_usd = max(0.0, self.reserved_capital_usd - amount)
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


def attempt_dex_trade(opp: Opportunity, pool: DexCapitalPool, gas_cost_usd: float, rng, now: float | None = None) -> DexTradeAttempt:
    """The full pipeline: capital check -> revalidation (spec item 7) ->
    fill/fail roll -> resolve. Every branch releases whatever capital it
    reserved before returning — the pool's invariant
    (available + reserved + deployed == total + realized_pnl) holds after
    every single call, not just eventually."""
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

    capital_usd = min(opp.capital_usd or 0.0, pool.available_capital_usd)
    if capital_usd <= 0 or not pool.reserve(capital_usd):
        return _result(DexTradeStatus.NO_CAPITAL_AVAILABLE, 0.0, 0.0, None, validation_timestamp, validation_timestamp)

    execution_attempt_timestamp = validation_timestamp  # transaction broadcast happens right after validation
    inclusion = build_execution_model(chain).estimate_inclusion()
    execution_complete_timestamp = execution_attempt_timestamp + inclusion.total_seconds

    still_valid, revalidated_net_pct = revalidate_edge(opp, chain, rng)
    if not still_valid:
        pool.release_reservation(capital_usd)
        return _result(DexTradeStatus.EDGE_DISAPPEARED, 0.0, 0.0, revalidated_net_pct, execution_attempt_timestamp, execution_complete_timestamp)

    fill_probability = opp.execution_fill_probability if opp.execution_fill_probability is not None else 1.0
    if rng.random() > fill_probability:
        # Attempted, inclusion/fill roll failed — gas is spent regardless
        # of outcome (broadcasting the transaction is what costs gas, not
        # its success), no profit. Never a principal loss beyond gas: see
        # module docstring — a more pessimistic non-atomic
        # leg-fails-after-the-other-fills model is a documented future
        # refinement, not built tonight.
        net_profit_usd = -gas_cost_usd
        pool.resolve(capital_usd, net_profit_usd)
        return _result(DexTradeStatus.FAILED, capital_usd, net_profit_usd, revalidated_net_pct, execution_attempt_timestamp, execution_complete_timestamp)

    # gas is NOT subtracted again here — revalidated_net_pct is derived from
    # opp.realistic_executable_edge_pct, which already had gas netted out
    # at detection time (app.onchain.cross_dex_arbitrage.evaluate_dex_capital_tier
    # / multihop_arbitrage.simulate_route both subtract gas_cost_usd before
    # producing that percentage) — subtracting it a second time here would
    # double-count the exact same cost.
    net_profit_usd = capital_usd * (revalidated_net_pct / 100)
    pool.resolve(capital_usd, net_profit_usd)
    return _result(DexTradeStatus.FILLED, capital_usd, net_profit_usd, revalidated_net_pct, execution_attempt_timestamp, execution_complete_timestamp)
