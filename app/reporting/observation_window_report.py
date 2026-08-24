"""V2.1 OBSERVATION WINDOW REPORT (user directive, 2026-08-24, item 9)
— combines full-universe discovery, missed-opportunity attribution,
capital-bottleneck simulation and inventory scoring into the exact
report format requested. Read-only, no order — real_orders_placed is
always 0.

Never forces a result: BEST_CURRENT_SYMBOL and INVENTORY_CANDIDATE
report None (rendered "NONE") when nothing genuinely qualifies — item
9's own words: "Ne place aucun ordre supplémentaire uniquement pour
produire des statistiques", "STOP avant toute activation de
l'Inventory Manager réel".
"""

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.execution.inventory_manager import build_inventory_report, is_preposition_eligible
from app.reporting.capital_bottleneck_report import build_capital_bottleneck_report
from app.reporting.full_universe_discovery_report import build_full_market_discovery_report
from app.reporting.missed_opportunity_report import build_missed_opportunity_report

DEFAULT_LOOKBACK_HOURS = 24.0
DEFAULT_MAX_RANKER_SYMBOLS = 30


@dataclass(slots=True)
class ObservationWindowReport:
    binance_bybit_opportunities_detected: int
    net_positive: int
    repeating: int
    executable_with_current_inventory: int
    missed_profitable: int
    primary_missed_reason: str | None
    best_current_symbol: str | None
    current_capital_bottleneck: bool
    would_300_materially_help: bool
    would_300_evidence: str
    would_500_materially_help: bool
    would_500_evidence: str
    inventory_candidate: str | None


def render_observation_window_text(report: ObservationWindowReport) -> str:
    lines = [
        f"BINANCE↔BYBIT OPPORTUNITIES DETECTED = {report.binance_bybit_opportunities_detected}",
        f"NET POSITIVE = {report.net_positive}",
        f"REPEATING = {report.repeating}",
        f"EXECUTABLE WITH CURRENT INVENTORY = {report.executable_with_current_inventory}",
        f"MISSED PROFITABLE = {report.missed_profitable}",
        f"PRIMARY MISSED REASON = {report.primary_missed_reason or 'NONE'}",
        f"BEST CURRENT SYMBOL = {report.best_current_symbol or 'NONE'}",
        f"CURRENT CAPITAL BOTTLENECK = {'YES' if report.current_capital_bottleneck else 'NO'}",
        f"WOULD 300 USDT MATERIALLY HELP = {'YES' if report.would_300_materially_help else 'NO'} — {report.would_300_evidence}",
        f"WOULD 500 USDT MATERIALLY HELP = {'YES' if report.would_500_materially_help else 'NO'} — {report.would_500_evidence}",
        f"INVENTORY CANDIDATE = {report.inventory_candidate or 'NONE'}",
    ]
    return "\n".join(lines)


async def build_observation_window_report(
    session: AsyncSession,
    max_ranker_symbols: int = DEFAULT_MAX_RANKER_SYMBOLS,
    min_expected_reuse_count: int | None = None,
    lookback_hours: float = DEFAULT_LOOKBACK_HOURS,
) -> ObservationWindowReport:
    settings = get_settings()
    reuse_count = min_expected_reuse_count if min_expected_reuse_count is not None else settings.min_expected_reuse_count

    discovery = await build_full_market_discovery_report(session, min_expected_reuse_count=reuse_count, lookback_hours=lookback_hours)
    missed = await build_missed_opportunity_report(session, max_ranker_symbols=max_ranker_symbols)
    capital = await build_capital_bottleneck_report(session, min_expected_reuse_count=reuse_count, lookback_hours=lookback_hours)
    inventory = await build_inventory_report(session, max_ranker_symbols=max_ranker_symbols)

    best_symbol = None
    if discovery.top_10_opportunities and discovery.top_10_opportunities[0].net_profit_per_1000usdt_mean > 0:
        best_symbol = discovery.top_10_opportunities[0].symbol

    eligible_scores = sorted((s for s in inventory.inventory_scores if is_preposition_eligible(s)), key=lambda s: s.total_score, reverse=True)
    inventory_candidate = eligible_scores[0].base_asset if eligible_scores else None

    return ObservationWindowReport(
        binance_bybit_opportunities_detected=discovery.pairs_deep_validated,
        net_positive=discovery.pairs_net_positive_stage_b_live,
        repeating=discovery.pairs_with_repeating_net_edge,
        executable_with_current_inventory=len(inventory.prepositioned_assets),
        missed_profitable=missed.total_missed,
        primary_missed_reason=missed.primary_cause,
        best_current_symbol=best_symbol,
        current_capital_bottleneck=capital.current_capital_bottleneck,
        would_300_materially_help=capital.would_300_materially_help,
        would_300_evidence=capital.would_300_evidence,
        would_500_materially_help=capital.would_500_materially_help,
        would_500_evidence=capital.would_500_evidence,
        inventory_candidate=inventory_candidate,
    )
