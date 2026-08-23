"""Global Capital Allocator — PAPER TRADING ONLY (Phase 2C, user
directive, 2026-08-23).

REAL decision authority over a SIMULATED $10,000 pool (CEX "5K" reference
portfolio + DEX $5,000 pool, unified — the exact pairing the user decided
on during Phase 1). Time-windowed reservation, the SAME proven shape
app.onchain.dex_paper_trader.DexCapitalPool and app.shadow.ledger.
ShadowCapitalLedger already use — deliberately reimplemented here rather
than imported, since this is a genuinely new object with its own real
behavioral effect (unlike the shadow ledger, which never influences
anything), and duplicating a ~40-line class is a small price for keeping
each one's blast radius independently reasoned-about.

CUTOVER_STRATEGIES is the empirically-validated set from the Phase 2B
final report (99.89% CEX scan-level agreement, 99.17% DEX agreement) —
cross_exchange (CEX) plus all 4 currently-attemptable DEX strategies.
Every other CEX strategy (triangular/stablecoin/funding/basis) was never
observed at scan-level granularity and stays entirely on OLD.
"""

import uuid
from dataclasses import dataclass, field

GLOBAL_CAPITAL_TOTAL_USD = 10_000.0  # CEX "5K" reference portfolio + DEX $5,000 pool

CUTOVER_STRATEGIES = frozenset({"cross_exchange", "atomic", "dex_triangular", "dex_multihop", "dex_cross"})

DEFAULT_HOLDING_SECONDS = 60.0  # fallback capital-lock duration when an opportunity carries none


@dataclass(slots=True)
class _Reservation:
    amount: float
    release_at: float
    engine: str  # "CEX" or "DEX" — for per-engine reporting only, not part of the safety invariant


@dataclass(slots=True)
class GlobalCapitalAllocator:
    total_capital_usd: float = GLOBAL_CAPITAL_TOTAL_USD
    realized_pnl_usd: float = 0.0
    _reservations: dict[uuid.UUID, _Reservation] = field(default_factory=dict)

    # Live session counters (user directive, spec Part "Dashboard") — reset
    # on process restart, same as the allocator's own capital state itself;
    # not a durable historical ledger, just what THIS run has decided.
    grants_count: int = 0
    rejections_count: int = 0
    fills_count: int = 0

    def _prune_expired(self, now: float) -> None:
        expired = [key for key, res in self._reservations.items() if res.release_at <= now]
        for key in expired:
            del self._reservations[key]

    def locked_capital_usd(self, now: float) -> float:
        self._prune_expired(now)
        return sum(res.amount for res in self._reservations.values())

    def locked_by_engine_usd(self, now: float, engine: str) -> float:
        self._prune_expired(now)
        return sum(res.amount for res in self._reservations.values() if res.engine == engine)

    def available_capital_usd(self, now: float) -> float:
        return self.total_capital_usd + self.realized_pnl_usd - self.locked_capital_usd(now)

    def reserve(self, reservation_id: uuid.UUID, amount: float, now: float, release_at: float, engine: str) -> bool:
        if amount <= 0 or amount > self.available_capital_usd(now) + 1e-9:
            return False
        self._reservations[reservation_id] = _Reservation(amount=amount, release_at=release_at, engine=engine)
        return True

    def adjust_reservation(self, reservation_id: uuid.UUID, actual_amount: float) -> None:
        """After the underlying (unmodified) executor determines the REAL
        capital it used — which can only be <= what MASTER granted, since
        the executor's own internal capital check (e.g. the "5K"
        portfolio's own available_usd(), or DexCapitalPool's own pool) is
        an additional, independent constraint layered UNDER MASTER's
        grant, never bypassed — shrinks the reservation to match. Frees
        the unused excess immediately rather than holding it locked for
        the full window nothing ended up using."""
        entry = self._reservations.get(reservation_id)
        if entry is not None and actual_amount < entry.amount:
            entry.amount = max(actual_amount, 0.0)

    def release_reservation(self, reservation_id: uuid.UUID) -> float | None:
        entry = self._reservations.pop(reservation_id, None)
        return entry.amount if entry else None

    def resolve_pnl(self, net_profit_usd: float) -> None:
        self.realized_pnl_usd += net_profit_usd

    def record_grant(self) -> None:
        self.grants_count += 1

    def record_rejection(self) -> None:
        self.rejections_count += 1

    def record_fill(self) -> None:
        self.fills_count += 1

    def check_invariant(self, now: float) -> list[str]:
        """The mandatory invariant (user directive): available + reserved
        == total + realized_pnl, and available must never go negative.
        Returns a list of violation descriptions — empty means healthy.
        Called after every reservation-affecting operation in main.py's
        wiring; any violation triggers an immediate rollback
        (app.orchestration.control.master_control.disable)."""
        violations: list[str] = []
        available = self.available_capital_usd(now)
        locked = self.locked_capital_usd(now)
        total = self.total_capital_usd + self.realized_pnl_usd
        if available < -1e-6:
            violations.append(f"available_capital_usd went negative: {available:.6f}")
        if abs((available + locked) - total) > 1e-6:
            violations.append(f"invariant broken: available({available:.6f}) + reserved({locked:.6f}) != total({total:.6f})")
        return violations


@dataclass(slots=True)
class CapitalGrant:
    amount: float
    release_at: float


# Continuous Execution spec's own convention (see app.risk.risk_engine's
# identical docstring) — one shared instance so main.py's detection loops
# and the FastAPI status/rollback endpoints (app/api/routes.py) are
# looking at the same allocator, without a circular import between them.
global_allocator = GlobalCapitalAllocator()


def try_reserve_for_opportunity(
    allocator: GlobalCapitalAllocator,
    opportunity_id: uuid.UUID,
    capital_requested_usd: float | None,
    holding_period_seconds: float | None,
    now: float,
    engine: str,
) -> CapitalGrant | None:
    """Sizes DOWN to whatever's actually available in the global pool —
    matching the already-established behavior of every real engine in
    this codebase (attempt_dex_trade, paper_trader.simulate,
    app.shadow.decision.evaluate_shadow_decision) — never an all-or-
    nothing reject when only PART of the requested capital is free.
    Returns None only when nothing at all can be granted."""
    if capital_requested_usd is None or capital_requested_usd <= 0:
        return None
    available = allocator.available_capital_usd(now)
    amount = min(capital_requested_usd, available)
    if amount <= 0:
        return None
    release_at = now + (holding_period_seconds or DEFAULT_HOLDING_SECONDS)
    if not allocator.reserve(opportunity_id, amount, now, release_at, engine):
        return None
    return CapitalGrant(amount=amount, release_at=release_at)
