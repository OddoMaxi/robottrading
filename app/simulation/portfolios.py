"""Virtual Portfolios (section 20) — no real capital, tracked balances only."""

from dataclasses import dataclass, field
from typing import Literal

from app.config.constants import VIRTUAL_PORTFOLIO_SIZES_USD

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

    def lock_capital(self, position_key: str, amount: float, expiry: float) -> None:
        self._locked[position_key] = (amount, expiry)

    def _prune_expired(self, now: float) -> None:
        expired_keys = [key for key, (_, expiry) in self._locked.items() if expiry <= now]
        for key in expired_keys:
            del self._locked[key]


def build_default_portfolios(capital_mode: CapitalMode = "compound") -> list[VirtualPortfolio]:
    return [
        VirtualPortfolio(name=name, initial_capital_usd=amount, balances={"USDT": amount}, capital_mode=capital_mode)
        for name, amount in VIRTUAL_PORTFOLIO_SIZES_USD.items()
    ]
