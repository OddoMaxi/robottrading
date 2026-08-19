"""Virtual Portfolios (section 20) — no real capital, tracked balances only."""

from dataclasses import dataclass, field

from app.config.constants import VIRTUAL_PORTFOLIO_SIZES_USD


@dataclass(slots=True)
class VirtualPortfolio:
    name: str
    initial_capital_usd: float
    balances: dict[str, float] = field(default_factory=dict)  # asset -> amount

    @property
    def current_value_usd(self) -> float:
        # V1: single quote-currency portfolios: total = cash balance until
        # the Rebalancing Engine + multi-exchange balances (section 21) are wired in.
        return sum(self.balances.values())


def build_default_portfolios() -> list[VirtualPortfolio]:
    return [
        VirtualPortfolio(name=name, initial_capital_usd=amount, balances={"USDT": amount})
        for name, amount in VIRTUAL_PORTFOLIO_SIZES_USD.items()
    ]
