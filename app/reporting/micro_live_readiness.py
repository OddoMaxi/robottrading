"""Micro Live Readiness (Reality Engine spec, sections 59-60).

Aggregates every safety/quality signal this system already computes into
one checklist and a single READY_FOR_CONTROLLED_TEST / NOT_READY verdict.
Every individual check already exists elsewhere (Ledger Integrity, Capital
Pool invariants, Reality Capture, Performance Metrics, Stress Testing,
Binance Testnet connectivity) — this module's only job is composing their
results into the spec's own checklist; nothing here is a new judgment call
about whether a trade is good, only whether the system as a whole is
healthy enough to even consider a controlled test.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.execution.exchange_client import ExchangeClient
from app.reporting.simple_summary import build_performance_metrics, build_reality_capture
from app.risk.risk_engine import RiskEngine
from app.simulation.ledger_integrity import check_ledger_integrity
from app.simulation.live_stress_test import run_live_stress_test
from app.simulation.portfolios import VirtualPortfolio

# Starting points per the spec's own qualitative language ("stable,"
# "acceptable," "positive"), not reverse-engineered from a stated formula —
# recalibrate after real observation, same posture as every other
# threshold introduced this session (e.g. the market data quality cadence
# multiples, the robustness score's own weighting).
MIN_REALITY_CAPTURE_RATIO_PCT = 50.0
MAX_ACCEPTABLE_DRAWDOWN_PCT = 20.0
MIN_ROBUSTNESS_SCORE = 50.0

# Reference portfolio for the P&L/drawdown/reality-capture checks below —
# matches the Urgent Audit spec's own "Test Final: 5,000 USDT" example
# rather than inventing a new reference size.
REFERENCE_PORTFOLIO_NAME = "5K"


class ReadinessVerdict(StrEnum):
    READY_FOR_CONTROLLED_TEST = "ready_for_controlled_test"
    NOT_READY = "not_ready"


@dataclass(slots=True)
class ReadinessCheck:
    name: str
    passed: bool
    detail: str


@dataclass(slots=True)
class ReadinessReport:
    verdict: ReadinessVerdict
    checks: list[ReadinessCheck] = field(default_factory=list)

    @property
    def failed_checks(self) -> list[ReadinessCheck]:
        return [c for c in self.checks if not c.passed]


def build_readiness_report(
    *,
    ledger_reconciled: bool,
    ledger_violations: list[str],
    any_available_capital_negative: bool,
    any_utilization_over_100_pct: bool,
    kill_switch_engaged: bool,
    reality_capture_ratio_pct: float | None,
    net_profit_usd: float | None,
    max_drawdown_pct: float | None,
    robustness_score: float | None,
    testnet_reachable: bool,
) -> ReadinessReport:
    """Pure aggregation step — every input is already-computed data from
    elsewhere, so this is unit-testable without touching a database or the
    network. A None value (no data yet) counts as failing its check rather
    than being skipped or treated as vacuously true: "not enough history to
    know" is not the same as "known to be fine."
    """
    checks = [
        ReadinessCheck(
            "ledger_healthy", ledger_reconciled, "reconciled" if ledger_reconciled else "; ".join(ledger_violations) or "not reconciled"
        ),
        ReadinessCheck(
            "no_negative_capital",
            not any_available_capital_negative,
            "available_usd >= 0 on every portfolio" if not any_available_capital_negative else "at least one portfolio is negative",
        ),
        ReadinessCheck(
            "no_over_allocation",
            not any_utilization_over_100_pct,
            "utilization <= 100% on every portfolio" if not any_utilization_over_100_pct else "at least one portfolio exceeds 100%",
        ),
        ReadinessCheck(
            "kill_switch_disengaged", not kill_switch_engaged, "not engaged" if not kill_switch_engaged else "currently engaged"
        ),
        ReadinessCheck(
            "reality_capture_stable",
            reality_capture_ratio_pct is not None and reality_capture_ratio_pct >= MIN_REALITY_CAPTURE_RATIO_PCT,
            f"{reality_capture_ratio_pct:.1f}% (needs >= {MIN_REALITY_CAPTURE_RATIO_PCT:.0f}%)"
            if reality_capture_ratio_pct is not None
            else "no trade data yet",
        ),
        ReadinessCheck(
            "positive_net_pnl",
            net_profit_usd is not None and net_profit_usd > 0,
            f"{net_profit_usd:.2f} USD" if net_profit_usd is not None else "no trade data yet",
        ),
        ReadinessCheck(
            "acceptable_drawdown",
            max_drawdown_pct is not None and max_drawdown_pct <= MAX_ACCEPTABLE_DRAWDOWN_PCT,
            f"{max_drawdown_pct:.1f}% (needs <= {MAX_ACCEPTABLE_DRAWDOWN_PCT:.0f}%)" if max_drawdown_pct is not None else "no data yet",
        ),
        ReadinessCheck(
            "stress_test_positive",
            robustness_score is not None and robustness_score >= MIN_ROBUSTNESS_SCORE,
            f"robustness score {robustness_score:.1f} (needs >= {MIN_ROBUSTNESS_SCORE:.0f})"
            if robustness_score is not None
            else "no stress test data yet",
        ),
        ReadinessCheck("testnet_reachable", testnet_reachable, "reachable" if testnet_reachable else "unreachable"),
    ]
    verdict = ReadinessVerdict.READY_FOR_CONTROLLED_TEST if all(c.passed for c in checks) else ReadinessVerdict.NOT_READY
    return ReadinessReport(verdict=verdict, checks=checks)


async def build_micro_live_readiness(
    session: AsyncSession,
    portfolios: list[VirtualPortfolio],
    portfolio_ids: dict[str, int],
    risk_engine: RiskEngine,
    exchange_client: ExchangeClient,
    now: datetime | None = None,
) -> ReadinessReport:
    ledger_reconciled = True
    ledger_violations: list[str] = []
    any_negative = False
    any_over_100 = False
    for portfolio in portfolios:
        check = await check_ledger_integrity(session, portfolio, portfolio_ids[portfolio.name], now=now)
        if not check.reconciled:
            ledger_reconciled = False
            ledger_violations.extend(f"{check.portfolio_name}: {v}" for v in check.violations)
        if check.available_usd < 0:
            # Mathematically the same condition as "utilization > 100%"
            # under this system's accounting (equity = available + engaged),
            # but tracked as two distinct checklist items per the spec —
            # matching how the Urgent Audit itself listed them separately.
            any_negative = True
            any_over_100 = True

    reference_portfolio_id = portfolio_ids.get(REFERENCE_PORTFOLIO_NAME)
    reality_capture_ratio_pct = None
    net_profit_usd = None
    max_drawdown_pct = None
    if reference_portfolio_id is not None:
        reference_portfolio = next((p for p in portfolios if p.name == REFERENCE_PORTFOLIO_NAME), None)
        capture = await build_reality_capture(session, reference_portfolio_id, now=now)
        if capture.trade_count > 0:
            reality_capture_ratio_pct = capture.capture_ratio_pct
            net_profit_usd = capture.realistic_usd
        if reference_portfolio is not None:
            metrics = await build_performance_metrics(session, reference_portfolio_id, reference_portfolio.initial_capital_usd, now=now)
            max_drawdown_pct = metrics.max_drawdown_pct

    stress_report = await run_live_stress_test(seed=0, now=None)
    robustness_score = stress_report.robustness_score if stress_report.results else None

    connectivity = await exchange_client.check_connectivity()

    return build_readiness_report(
        ledger_reconciled=ledger_reconciled,
        ledger_violations=ledger_violations,
        any_available_capital_negative=any_negative,
        any_utilization_over_100_pct=any_over_100,
        kill_switch_engaged=risk_engine.kill_switch_engaged,
        reality_capture_ratio_pct=reality_capture_ratio_pct,
        net_profit_usd=net_profit_usd,
        max_drawdown_pct=max_drawdown_pct,
        robustness_score=robustness_score,
        testnet_reachable=connectivity.reachable,
    )
