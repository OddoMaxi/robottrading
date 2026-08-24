"""INVENTORY MANAGER V2 — FINAL REPORT (user directive, 2026-08-24, item
10). Combines app.reporting.full_universe_discovery_report (STAGE A/B
counters, top opportunities) with app.execution.inventory_manager
(classification, rebalance recommendations) into the exact report shape
requested. Read-only, no order — real_inventory_orders is always 0.

READY TO ENABLE AUTOMATIC REAL INVENTORY MANAGEMENT is hardcoded False
in this build, not derived from data quality: no order-placement code
path exists anywhere in app.execution.inventory_manager (see
tests/test_inventory_manager_isolation.py), and enabling real execution
is explicitly a separate, later, human-authorized step per the user's
own non-negotiable constraint — no amount of good data makes that step
automatic (see [[feedback_capital_authorization_gating]] discipline
applied throughout this project: Phase 3A/3's readiness gates always
required a final human go-ahead too, regardless of verdict).
"""

from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.inventory_manager import InventoryClassification, InventoryManagerReport, RebalanceRecommendation, build_inventory_report
from app.reporting.full_universe_discovery_report import FullMarketDiscoveryReport, build_full_market_discovery_report


@dataclass(slots=True)
class InventoryManagerV2FinalReport:
    common_universe: int
    pairs_actually_scanned: int
    pairs_raw_spread_stage_a: int  # see FullMarketDiscoveryReport's own docstring — STAGE A estimate, Binance/Bybit only
    pairs_net_positive_stage_b_live: int  # STAGE B real result, market_scope="live" only
    pairs_with_repeating_net_edge: int
    top_10_symbols: list[str] = field(default_factory=list)
    strong_inventory_candidates: list[str] = field(default_factory=list)
    recommended_bybit_inventory: list[RebalanceRecommendation] = field(default_factory=list)
    recommended_binance_inventory: list[RebalanceRecommendation] = field(default_factory=list)
    total_recommended_capital_locked_usdt: float = 0.0
    real_inventory_orders: int = 0
    ready_to_enable_automatic_real_inventory_management: bool = False
    ready_reason: str = ""
    inventory_manager_mode: str = "SIMULATION"
    auto_real_rebalance: bool = False
    discovery: FullMarketDiscoveryReport | None = None
    inventory: InventoryManagerReport | None = None


def _format_top10(discovery: FullMarketDiscoveryReport) -> list[str]:
    return [f"{s.symbol} {s.buy_exchange}→{s.sell_exchange} ({s.net_profit_per_1000usdt_mean:.2f}/1000usdt)" for s in discovery.top_10_opportunities]


def render_v2_report_text(report: InventoryManagerV2FinalReport) -> str:
    def _list_or_none(items: list[str]) -> str:
        return ", ".join(items) if items else "NONE"

    def _recs_or_no_action(recs: list[RebalanceRecommendation]) -> str:
        if not recs:
            return "NONE / NO ACTION"
        return "; ".join(f"{r.asset} {r.recommended_notional_usdt:.2f} USDT ({r.classification})" for r in recs)

    lines = [
        f"COMMON UNIVERSE = {report.common_universe}",
        f"PAIRS ACTUALLY SCANNED = {report.pairs_actually_scanned}",
        f"PAIRS WITH RAW EDGE (STAGE A, Binance/Bybit estimate) = {report.pairs_raw_spread_stage_a}",
        f"PAIRS NET POSITIVE AFTER COSTS (STAGE B, Binance/Bybit real) = {report.pairs_net_positive_stage_b_live}",
        f"PAIRS WITH REPEATING NET EDGE = {report.pairs_with_repeating_net_edge}",
        f"TOP 10 SYMBOLS = {_list_or_none(report.top_10_symbols)}",
        f"STRONG INVENTORY CANDIDATES = {_list_or_none(report.strong_inventory_candidates)}",
        f"RECOMMENDED BYBIT INVENTORY = {_recs_or_no_action(report.recommended_bybit_inventory)}",
        f"RECOMMENDED BINANCE INVENTORY = {_recs_or_no_action(report.recommended_binance_inventory)}",
        f"TOTAL RECOMMENDED CAPITAL LOCKED = {report.total_recommended_capital_locked_usdt:.2f} USDT",
        f"REAL INVENTORY ORDERS = {report.real_inventory_orders}",
        f"READY TO ENABLE AUTOMATIC REAL INVENTORY MANAGEMENT = {'YES' if report.ready_to_enable_automatic_real_inventory_management else 'NO'} ({report.ready_reason})",
    ]
    return "\n".join(lines)


async def build_inventory_manager_v2_report(
    session: AsyncSession,
    max_ranker_symbols: int = 30,
    min_expected_reuse_count: int | None = None,
) -> InventoryManagerV2FinalReport:
    from app.config.settings import get_settings

    settings = get_settings()
    reuse_count = min_expected_reuse_count if min_expected_reuse_count is not None else settings.min_expected_reuse_count

    discovery = await build_full_market_discovery_report(session, min_expected_reuse_count=reuse_count)
    inventory = await build_inventory_report(session, max_ranker_symbols=max_ranker_symbols)

    strong = sorted({s.base_asset for s in inventory.inventory_scores if s.classification == InventoryClassification.STRONG_PREPOSITION_CANDIDATE})
    buys = [r for r in inventory.rebalance_candidates if r.action == "BUY_INVENTORY"]
    bybit_buys = [r for r in buys if r.exchange == "bybit"]
    binance_buys = [r for r in buys if r.exchange == "binance"]
    total_locked = sum(r.capital_required_usdt for r in buys)

    return InventoryManagerV2FinalReport(
        common_universe=discovery.common_pairs,
        pairs_actually_scanned=discovery.pairs_fast_scanned,
        pairs_raw_spread_stage_a=discovery.pairs_raw_spread_stage_a,
        pairs_net_positive_stage_b_live=discovery.pairs_net_positive_stage_b_live,
        pairs_with_repeating_net_edge=discovery.pairs_with_repeating_net_edge,
        top_10_symbols=_format_top10(discovery),
        strong_inventory_candidates=strong,
        recommended_bybit_inventory=bybit_buys,
        recommended_binance_inventory=binance_buys,
        total_recommended_capital_locked_usdt=round(total_locked, 2),
        real_inventory_orders=0,
        ready_to_enable_automatic_real_inventory_management=False,
        ready_reason="simulation-only build: no order-placement path exists in app.execution.inventory_manager yet — enabling real execution is a separate, explicitly-authorized future step, not a data-driven verdict",
        inventory_manager_mode=settings.inventory_manager_mode,
        auto_real_rebalance=settings.auto_real_rebalance,
        discovery=discovery,
        inventory=inventory,
    )
