"""Virtual Portfolios (section 20) — no real capital, tracked balances only."""

import logging
from dataclasses import dataclass, field
from typing import Literal

from app.config.constants import VIRTUAL_PORTFOLIO_SIZES_USD

logger = logging.getLogger(__name__)

# Fast-Rotation spec, section 32.
CapitalMode = Literal["fixed", "compound"]


@dataclass(slots=True)
class VirtualPortfolio:
    name: str
    initial_capital_usd: float
    balances: dict[str, float] = field(default_factory=dict)  # asset -> amount
    # "fixed": per-trade sizing always references initial_capital_usd, so a
    # profitable run doesn't let position sizes creep up. "compound": sizing
    # references the current (profit-inclusive) balance, so gains compound
    # into bigger positions — spec section 32's two reinvestment modes.
    capital_mode: CapitalMode = "compound"
    # Capital tied up in open hold-based positions (Basis/Funding), keyed by
    # "strategy:exchange:symbol" -> (amount_usd, expiry_epoch_seconds). A
    # portfolio can only deploy capital it doesn't already have committed
    # elsewhere — without this, a $25,000 portfolio would happily "open"
    # unlimited $1,000 positions in parallel with no regard for its actual size.
    _locked: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def current_value_usd(self) -> float:
        # V1: single quote-currency portfolios: total = cash balance until
        # the Rebalancing Engine + multi-exchange balances (section 21) are wired in.
        return sum(self.balances.values())

    @property
    def reference_capital_usd(self) -> float:
        """What per-trade sizing (risk limits, % caps) is computed against."""
        return self.initial_capital_usd if self.capital_mode == "fixed" else self.current_value_usd

    def available_usd(self, now: float) -> float:
        self._prune_expired(now)
        locked_total = sum(amount for amount, _ in self._locked.values())
        return max(0.0, self.current_value_usd - locked_total)

    def open_position_count(self, now: float) -> int:
        """Continuous Execution spec, section 27 — how many positions this
        portfolio currently has open, for enforcing max_concurrent_trades."""
        self._prune_expired(now)
        return len(self._locked)

    def lock_capital(self, position_key: str, amount: float, expiry: float, now: float) -> bool:
        """Urgent audit fix, section 1 — capital reservation must be atomic
        and available_capital must never go negative. Returns False (and
        reserves nothing) if `amount` exceeds what's actually free right
        now, instead of blindly trusting the caller's sizing math. Checked
        here — not just at the caller — because state recovery
        (app.simulation.state_recovery) reconstructs locks from historical
        trades that may have been sized under since-superseded risk rules;
        this is the one place that can't be bypassed.

        Re-locking an already-locked key (e.g. state recovery re-applying
        the same position) releases its old amount first, so the check is
        against what's free *excluding* this key's own prior reservation,
        not a double-count.
        """
        self._prune_expired(now)
        previous_amount = self._locked.get(position_key, (0.0, 0.0))[0]
        locked_total_excluding_this_key = sum(a for a, _ in self._locked.values()) - previous_amount
        available_excluding_this_key = self.current_value_usd - locked_total_excluding_this_key
        if amount > available_excluding_this_key + 1e-6:  # epsilon for float rounding
            logger.warning(
                "capital reservation rejected for %s: requested %.2f, only %.2f available (invariant guard, spec section 1)",
                position_key,
                amount,
                max(0.0, available_excluding_this_key),
            )
            return False
        self._locked[position_key] = (amount, expiry)
        return True

    def _prune_expired(self, now: float) -> None:
        expired_keys = [key for key, (_, expiry) in self._locked.items() if expiry <= now]
        for key in expired_keys:
            del self._locked[key]


def build_default_portfolios(capital_mode: CapitalMode = "compound") -> list[VirtualPortfolio]:
    return [
        VirtualPortfolio(name=name, initial_capital_usd=amount, balances={"USDT": amount}, capital_mode=capital_mode)
        for name, amount in VIRTUAL_PORTFOLIO_SIZES_USD.items()
    ]
