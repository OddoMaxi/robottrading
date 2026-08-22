"""Shadow Global Capital Ledger (Phase 2, SHADOW MODE ONLY).

A theoretical, entirely-separate capital pool the Master Ranker allocates
against in simulation — never real money, never the same object as
app.simulation.portfolios.VirtualPortfolio or
app.onchain.dex_paper_trader.DexCapitalPool, and this module does not
import either of those (see app/shadow/__init__.py and
tests/test_shadow_isolation.py). Deliberately a small, standalone
reimplementation of the same time-windowed-reservation shape
DexCapitalPool already proved correct during the Reality Audit (spec
Part 7's own finding: synchronous reserve/resolve lets two opportunities
in the same instant each see the "full" pool) — duplicated here on
purpose, not refactored into a shared base class, so this module's
correctness and isolation can be verified independent of any change to
the real engines' own capital code.

Total capital mirrors the unified $10,000 view (CEX "5K" reference +
DEX $5,000 pool) the user explicitly decided on during Phase 1.
"""

import uuid
from dataclasses import dataclass, field

SHADOW_TOTAL_CAPITAL_USD = 10_000.0  # CEX "5K" reference + DEX $5,000 pool, per the Phase 1 unification decision


@dataclass(slots=True)
class _ShadowReservation:
    amount: float
    release_at: float


@dataclass(slots=True)
class ShadowCapitalLedger:
    total_capital_usd: float = SHADOW_TOTAL_CAPITAL_USD
    realized_pnl_usd: float = 0.0
    _reservations: dict[uuid.UUID, _ShadowReservation] = field(default_factory=dict)

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
        self._reservations[reservation_id] = _ShadowReservation(amount=amount, release_at=release_at)
        return True

    def resolve_pnl(self, net_profit_usd: float) -> None:
        self.realized_pnl_usd += net_profit_usd
