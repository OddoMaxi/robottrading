"""CAPITAL BOTTLENECK ANALYSIS (V2.1, user directive, 2026-08-24, item
6) — replays the SAME observed LIVE (Binance/Bybit) net-positive
opportunities against four capital levels (160/300/500/1000 USDT) to
answer, objectively: would more capital actually let us execute more
trades, or is something else (opportunity scarcity, inventory) the real
constraint? No real money moves and no order is placed anywhere in this
module — it is pure replay over already-persisted history.

Stated modeling assumptions (deliberately explicit, not hidden in code):

1. Per-leg notional stays fixed at settings.max_notional_per_leg_usdt
   regardless of capital tier — this mirrors the user's own Phase 3
   directive (INITIAL_MAX_NOTIONAL_PER_LEG=5 USDT, a deliberately small,
   controlled size) and the actual question being asked: "could we run
   MORE trades in parallel with more capital", not "could we size each
   trade bigger". Capital growth is modeled as more CONCURRENT ~5 USDT
   positions, not bigger ones.
2. Each capital tier splits Binance/Bybit in the SAME proportion as the
   user's own stated 100:60 target (5:3) — scaled up, never re-split
   arbitrarily.
3. A symbol's inventory readiness (whether the sell-side base asset
   would actually be pre-positioned) is evaluated with the SAME
   classification app.execution.inventory_manager already uses
   (PREPOSITION_CANDIDATE / STRONG_PREPOSITION_CANDIDATE) — a bigger
   capital pool is assumed to support proportionally more inventory
   pre-positioning budget too (MAX_TOTAL_INVENTORY_EXPOSURE_USDT scales
   with capital in spirit), but the ELIGIBILITY bar itself (real,
   repeating, consistent net edge) never loosens — item 4: "ne pas
   simplement baisser les seuils".
4. "Executable" opportunities are counted per DISTINCT (symbol,
   direction) — the best direction per symbol observed in the lookback
   window — not per individual scan tick, so a frequently-observed
   symbol doesn't inflate the count just for being scanned often.
"""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.execution.inventory_manager import is_preposition_eligible, score_direction_for_inventory
from app.reporting.altcoin_scan_report import DirectionSummary, build_altcoin_scan_report

CAPITAL_TIERS_USDT = (160.0, 300.0, 500.0, 1000.0)
DEFAULT_LOOKBACK_HOURS = 24.0


@dataclass(slots=True)
class CapitalTierResult:
    total_capital_usdt: float
    binance_allocation_usdt: float
    bybit_allocation_usdt: float
    executable_profitable_opportunities: int
    missed_for_capital: int
    missed_for_inventory: int
    capital_utilization_pct: float
    simulated_net_pnl_usd: float


@dataclass(slots=True)
class CapitalBottleneckReport:
    tiers: list[CapitalTierResult] = field(default_factory=list)
    current_capital_bottleneck: bool = False
    would_300_materially_help: bool = False
    would_300_evidence: str = ""
    would_500_materially_help: bool = False
    would_500_evidence: str = ""


def simulate_capital_tier(
    candidates: list[DirectionSummary],
    inventory_eligible_base_assets: set[str],
    total_capital_usdt: float,
    binance_ratio: float,
    bybit_ratio: float,
    max_notional_per_leg_usdt: float,
) -> CapitalTierResult:
    """Pure function, no I/O. candidates should already be filtered to
    net-positive, deduplicated best-direction-per-symbol, LIVE
    (Binance/Bybit) scope."""
    binance_allocation = total_capital_usdt * binance_ratio
    bybit_allocation = total_capital_usdt * bybit_ratio
    binance_slots = int(binance_allocation // max_notional_per_leg_usdt) if max_notional_per_leg_usdt > 0 else 0
    bybit_slots = int(bybit_allocation // max_notional_per_leg_usdt) if max_notional_per_leg_usdt > 0 else 0

    binance_candidates = sorted((c for c in candidates if c.buy_exchange == "binance"), key=lambda c: c.net_profit_per_1000usdt_mean, reverse=True)
    bybit_candidates = sorted((c for c in candidates if c.buy_exchange == "bybit"), key=lambda c: c.net_profit_per_1000usdt_mean, reverse=True)

    executable = binance_candidates[:binance_slots] + bybit_candidates[:bybit_slots]
    missed_for_capital = max(0, len(binance_candidates) - binance_slots) + max(0, len(bybit_candidates) - bybit_slots)
    missed_for_inventory = sum(1 for c in executable if c.symbol.split("/")[0] not in inventory_eligible_base_assets)

    total_slots = binance_slots + bybit_slots
    utilization_pct = (len(executable) / total_slots * 100) if total_slots > 0 else 0.0
    simulated_pnl = sum(c.net_profit_per_1000usdt_mean * (max_notional_per_leg_usdt / 1000.0) for c in executable)

    return CapitalTierResult(
        total_capital_usdt=total_capital_usdt,
        binance_allocation_usdt=round(binance_allocation, 2),
        bybit_allocation_usdt=round(bybit_allocation, 2),
        executable_profitable_opportunities=len(executable),
        missed_for_capital=missed_for_capital,
        missed_for_inventory=missed_for_inventory,
        capital_utilization_pct=round(utilization_pct, 1),
        simulated_net_pnl_usd=round(simulated_pnl, 4),
    )


def _evidence(base: CapitalTierResult, tier: CapitalTierResult, tier_label: str) -> tuple[bool, str]:
    helps = tier.executable_profitable_opportunities > base.executable_profitable_opportunities
    if helps:
        evidence = (
            f"{base.missed_for_capital} opportunity(ies) capital-blocked at {base.total_capital_usdt:.0f} USDT; "
            f"executable rises {base.executable_profitable_opportunities} -> {tier.executable_profitable_opportunities} at {tier_label}"
        )
    else:
        evidence = (
            f"executable stays at {base.executable_profitable_opportunities} opportunity(ies) — capital was not the "
            f"binding constraint at {base.total_capital_usdt:.0f} USDT (missed_for_inventory={base.missed_for_inventory}, "
            f"real opportunity count itself is the limit)"
        )
    return helps, evidence


async def build_capital_bottleneck_report(
    session: AsyncSession, min_expected_reuse_count: int | None = None, lookback_hours: float = DEFAULT_LOOKBACK_HOURS
) -> CapitalBottleneckReport:
    settings = get_settings()
    reuse_count = min_expected_reuse_count if min_expected_reuse_count is not None else settings.min_expected_reuse_count

    since = (datetime.now(UTC) - timedelta(hours=lookback_hours)).replace(tzinfo=None)
    scan_report = await build_altcoin_scan_report(session, since=since)  # market_scope="live" by default

    candidates = [s for s in scan_report.best_direction_by_symbol if s.net_profit_per_1000usdt_mean > 0]
    inventory_eligible = {
        s.symbol.split("/")[0]
        for s in scan_report.best_direction_by_symbol
        if is_preposition_eligible(score_direction_for_inventory(s, reuse_count))
    }

    total_target = settings.total_real_capital_usdt
    binance_ratio = settings.binance_target_capital_usdt / total_target if total_target > 0 else 0.5
    bybit_ratio = settings.bybit_target_capital_usdt / total_target if total_target > 0 else 0.5

    tiers = [
        simulate_capital_tier(candidates, inventory_eligible, capital, binance_ratio, bybit_ratio, settings.max_notional_per_leg_usdt)
        for capital in CAPITAL_TIERS_USDT
    ]

    base_tier = tiers[0]
    would_300, evidence_300 = _evidence(base_tier, tiers[1], "300 USDT")
    would_500, evidence_500 = _evidence(base_tier, tiers[2], "500 USDT")

    return CapitalBottleneckReport(
        tiers=tiers,
        current_capital_bottleneck=base_tier.missed_for_capital > 0,
        would_300_materially_help=would_300,
        would_300_evidence=evidence_300,
        would_500_materially_help=would_500,
        would_500_evidence=evidence_500,
    )
