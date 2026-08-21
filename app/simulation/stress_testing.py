"""Stress Testing (Reality Engine spec, section 46-47).

Replays the same recorded/synthetic dataset through three execution
conditions of increasing harshness — Normal, Stress1, Stress2 — and reports
how much of the Normal-condition P&L survives. A strategy whose profit
comes only from the Reality Engine's most optimistic assumptions (best-case
latency, zero leg failure beyond the baseline rate) is exactly the kind of
"looks profitable, isn't real" result the whole V5 spec exists to catch.

Stress1/Stress2 widen latency (via LatencyProfile — this replay's existing
knob) and additionally penalize fees, since section 46 calls out worse fees
under stress alongside slower/wider latency. Slippage/leg-failure themselves
aren't separately multiplied: they're already sampled from the same RNG
seed, and wider latency alone drives more MISSED/failed revalidations,
which is the dominant stress effect PaperTrader actually models today.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.execution.latency_engine import LatencyProfile
from app.simulation.portfolios import VirtualPortfolio
from app.simulation.replay_engine import MarketEvent, ReplayResult, run_replay


class StressScenario(StrEnum):
    NORMAL = "normal"
    STRESS_1 = "stress_1"
    STRESS_2 = "stress_2"


_SCENARIO_LATENCY: dict[StressScenario, LatencyProfile] = {
    StressScenario.NORMAL: LatencyProfile.OPTIMISTIC,
    StressScenario.STRESS_1: LatencyProfile.REALISTIC,
    StressScenario.STRESS_2: LatencyProfile.STRESS,
}


@dataclass(slots=True)
class StressTestReport:
    results: dict[StressScenario, ReplayResult]

    @property
    def net_profit_by_scenario_usd(self) -> dict[StressScenario, float]:
        return {scenario: result.net_profit_usd for scenario, result in self.results.items()}

    @property
    def robustness_score(self) -> float:
        normal_pnl = self.results[StressScenario.NORMAL].net_profit_usd
        stress_pnl = [self.results[StressScenario.STRESS_1].net_profit_usd, self.results[StressScenario.STRESS_2].net_profit_usd]
        return compute_robustness_score(normal_pnl, stress_pnl)


async def run_stress_test(
    events: list[MarketEvent],
    build_engines,
    make_portfolio,
    *,
    seed: int,
    replay_start: float,
) -> StressTestReport:
    """`make_portfolio` is a zero-arg factory (not a single shared
    VirtualPortfolio) — each scenario needs its own fresh balance/lock
    state, since PaperTrader mutates the portfolio it's given."""
    results = {}
    for scenario, latency_profile in _SCENARIO_LATENCY.items():
        results[scenario] = await run_replay(
            events, build_engines, make_portfolio(), seed=seed, replay_start=replay_start, latency_profile=latency_profile
        )
    return StressTestReport(results)


def compute_robustness_score(normal_pnl_usd: float, stress_pnl_usd: list[float]) -> float:
    """0-100. Whether a scenario stays profitable at all matters far more
    than exactly how much of the Normal P&L it retains — a strategy that
    goes negative under any stress condition is not robust regardless of
    how good its Normal-condition number looks, so that costs half the
    score outright rather than just shrinking an average.

    If Normal itself isn't profitable there's nothing to be robust *about*
    — scored 0 rather than divided-by-zero or treated as trivially perfect.
    """
    if normal_pnl_usd <= 0 or not stress_pnl_usd:
        return 0.0

    retentions = [max(0.0, min(1.0, pnl / normal_pnl_usd)) for pnl in stress_pnl_usd]
    avg_retention_score = (sum(retentions) / len(retentions)) * 100

    survived_all = all(pnl > 0 for pnl in stress_pnl_usd)
    if not survived_all:
        avg_retention_score *= 0.5

    return round(avg_retention_score, 1)
