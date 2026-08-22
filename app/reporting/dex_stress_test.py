"""Stress Test / Adverse Scenario Replay (V5.5 REALITY AUDIT, user
directive, 2026-08-22, section 6).

"Trois scenarios de stress: BASE / +25% gas+slippage+250ms latence /
+50%+50%+500ms / combinaison adverse realiste — chacun avec le jeu complet
de metriques." This module replays a list of historical, ALREADY-DETECTED
opportunities (the same snapshotted legs app.reporting.dex_replay and
app.onchain.dex_paper_trader.resize_at_attempt already reconstruct
DexPools from) through the real, unmodified pricing/revalidation math —
evaluate_dex_capital_tier and the same drift-over-inclusion-latency model
attempt_dex_trade uses — but with gas, slippage, and latency inflated by
the scenario's stated multipliers. This is deliberately NOT a second,
duplicated pricing implementation: the only thing that changes between
scenarios is the numbers fed into the same functions production already
runs.

Only covers the plain 2-leg dex_cross shape (legs carry price/tvl_usd/
fee_pct) — the same scope limit as dex_replay and resize_at_attempt.
Historical gas_cost_usd is NOT persisted per-opportunity (only the
already-net realistic_executable_edge_pct is), so BASE gas is a supplied,
documented assumption rather than an exact historical replay of what gas
was at detection time — callers must pass it explicitly rather than this
module fabricating a number silently.
"""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord
from app.onchain.constants import SLIPPAGE_BUFFER_PCT
from app.onchain.cross_dex_arbitrage import evaluate_dex_capital_tier
from app.onchain.dex_paper_trader import PRICE_DRIFT_STD_PCT_PER_SQRT_SECOND
from app.onchain.execution_model import build_execution_model
from app.onchain.models import DexPool


@dataclass(slots=True)
class StressScenario:
    name: str
    gas_multiplier: float
    slippage_multiplier: float
    extra_latency_seconds: float


BASE = StressScenario(name="BASE", gas_multiplier=1.0, slippage_multiplier=1.0, extra_latency_seconds=0.0)
STRESS_1 = StressScenario(name="STRESS1_+25pct_gas_slippage_250ms", gas_multiplier=1.25, slippage_multiplier=1.25, extra_latency_seconds=0.25)
STRESS_2 = StressScenario(name="STRESS2_+50pct_gas_slippage_500ms", gas_multiplier=1.5, slippage_multiplier=1.5, extra_latency_seconds=0.5)
# "Combinaison adverse realiste" — not simply the STRESS2 numbers again:
# gas spikes are usually short-lived and don't always coincide with the
# worst slippage moment, but a genuinely adverse scenario compounds a
# realistic worst case on each dimension independently rather than
# applying one uniform multiplier — documented assumption, not a
# measurement (no historical joint-distribution data exists yet).
ADVERSE_COMBO = StressScenario(name="ADVERSE_COMBO", gas_multiplier=2.0, slippage_multiplier=1.75, extra_latency_seconds=1.0)

ALL_SCENARIOS = [BASE, STRESS_1, STRESS_2, ADVERSE_COMBO]


@dataclass(slots=True)
class StressScenarioResult:
    scenario: str
    n_opportunities: int
    n_skipped_unsupported_shape: int
    n_still_profitable: int
    n_edge_disappeared: int
    n_not_profitable_at_size: int
    total_net_profit_usd: float
    avg_net_pct: float | None
    capture_ratio_pct: float | None  # still_profitable / (n_opportunities - n_skipped)


def _reconstruct_pools(legs: list[dict]) -> tuple[DexPool, DexPool] | None:
    if len(legs) != 2 or not all("price" in leg and "tvl_usd" in leg and "fee_pct" in leg and "pool_id" in leg and "chain" in leg and "exchange" in leg for leg in legs):
        return None
    buy_leg, sell_leg = legs[0], legs[1]
    buy_pool = DexPool(
        chain=buy_leg["chain"], dex=buy_leg["exchange"], pool_id=buy_leg["pool_id"],
        token0_symbol="A", token1_symbol="B", price=buy_leg["price"], tvl_usd=buy_leg["tvl_usd"],
        volume_24h_usd=0.0, fee_pct=buy_leg["fee_pct"], pool_created_at=None, last_update=0.0,
    )
    sell_pool = DexPool(
        chain=sell_leg["chain"], dex=sell_leg["exchange"], pool_id=sell_leg["pool_id"],
        token0_symbol="A", token1_symbol="B", price=sell_leg["price"], tvl_usd=sell_leg["tvl_usd"],
        volume_24h_usd=0.0, fee_pct=sell_leg["fee_pct"], pool_created_at=None, last_update=0.0,
    )
    return buy_pool, sell_pool


def simulate_stress_scenario(
    opportunities: list[dict],
    scenario: StressScenario,
    base_gas_cost_usd: float,
    rng,
) -> StressScenarioResult:
    """Each opportunity dict must have: legs (list[dict]), capital_usd
    (float, the detection-time size to replay at — this module does NOT
    re-run Smart Position Sizing, it stress-tests the SAME size that was
    actually persisted, per spec item 6's own framing of "would this
    specific opportunity still have worked")."""
    n_skipped = 0
    n_profitable = 0
    n_edge_disappeared = 0
    n_not_profitable = 0
    total_net_profit = 0.0
    net_pcts: list[float] = []

    stressed_gas = base_gas_cost_usd * scenario.gas_multiplier
    stressed_slippage_pct = SLIPPAGE_BUFFER_PCT * scenario.slippage_multiplier

    for opp in opportunities:
        pools = _reconstruct_pools(opp["legs"])
        if pools is None:
            n_skipped += 1
            continue
        buy_pool, sell_pool = pools
        chain = opp["legs"][0]["chain"]
        capital_usd = float(opp["capital_usd"])

        inclusion = build_execution_model(chain).estimate_inclusion()
        total_latency_seconds = inclusion.total_seconds + scenario.extra_latency_seconds
        drift_std_pct = PRICE_DRIFT_STD_PCT_PER_SQRT_SECOND * (total_latency_seconds**0.5)
        drift_pct = rng.gauss(0.0, drift_std_pct)

        tier = evaluate_dex_capital_tier(
            buy_pool, sell_pool, buy_pool.price, sell_pool.price, capital_usd, stressed_gas, slippage_buffer_pct=stressed_slippage_pct
        )
        stressed_net_pct = tier.net_pct + drift_pct

        if stressed_net_pct <= 0:
            n_edge_disappeared += 1
            continue

        stressed_net_profit_usd = capital_usd * (stressed_net_pct / 100)
        if stressed_net_profit_usd <= 0:
            n_not_profitable += 1
            continue

        n_profitable += 1
        total_net_profit += stressed_net_profit_usd
        net_pcts.append(stressed_net_pct)

    considered = len(opportunities) - n_skipped
    return StressScenarioResult(
        scenario=scenario.name,
        n_opportunities=len(opportunities),
        n_skipped_unsupported_shape=n_skipped,
        n_still_profitable=n_profitable,
        n_edge_disappeared=n_edge_disappeared,
        n_not_profitable_at_size=n_not_profitable,
        total_net_profit_usd=total_net_profit,
        avg_net_pct=(sum(net_pcts) / len(net_pcts)) if net_pcts else None,
        capture_ratio_pct=(n_profitable / considered * 100) if considered else None,
    )


async def fetch_dex_cross_opportunities_with_price_snapshot(session: AsyncSession, since: datetime) -> list[dict]:
    """Live-data source for the dashboard's Stress Test panel (spec Part
    AD) — never hardcode a past manual run's numbers; this always reflects
    whatever dex_cross opportunities actually exist in the ledger right
    now. Same 2-leg, price/tvl/fee-snapshot scope limit as the rest of
    this module."""
    rows = (
        await session.execute(
            select(OpportunityRecord.legs, OpportunityRecord.capital_usd).where(
                OpportunityRecord.strategy == "dex_cross",
                OpportunityRecord.detected_at >= since,
                OpportunityRecord.capital_usd.is_not(None),
            )
        )
    ).all()
    return [{"legs": legs, "capital_usd": float(capital_usd)} for legs, capital_usd in rows if legs and len(legs) == 2 and "price" in legs[0]]
