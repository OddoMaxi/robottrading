"""Rebalancing Engine (section 22) — cost of restoring balance across exchanges."""

from dataclasses import dataclass


@dataclass(slots=True)
class RebalancingPlan:
    portfolio_name: str
    from_exchange: str
    to_exchange: str
    asset: str
    amount: float
    network_fee_usd: float
    estimated_duration_seconds: float

    @property
    def total_cost_usd(self) -> float:
        # V1: network fee only. Capital-immobilization opportunity cost
        # (section 22) needs a yield/return baseline to price against — added
        # once enough paper-trading history exists to estimate it.
        return self.network_fee_usd


class RebalancingEngine:
    def plan(self, portfolio_name: str, from_exchange: str, to_exchange: str, asset: str, amount: float) -> RebalancingPlan:
        raise NotImplementedError("Needs live network fee data per asset/exchange")
