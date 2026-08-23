"""LIVE VALIDATION SCORE (Phase 3, user directive, 2026-08-23) — READ-ONLY.

"Nous voulons d'abord prouver que predicted profitable trade ≈ actual
profitable trade sur plusieurs exécutions réelles" before any capital
increase. This module answers exactly that question from the Profit
Reality Ledger (live_arbitrage_executions) — it can RECOMMEND
ELIGIBLE_FOR_SIZE_INCREASE, but never raises max_notional_per_leg_usdt
itself; that remains an operator's explicit act (app.config.settings /
.env), never automatic.
"""

import statistics
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import LiveArbitrageExecutionRecord

# Deliberately conservative, stated up front — not fitted to produce a
# favorable recommendation. Recalibrate only with a stated reason.
MIN_COMPLETED_TRADES_TO_JUDGE = 10
MIN_PROFITABLE_RATE_PCT = 80.0
MAX_MEAN_ABS_PREDICTION_ERROR_USD = 0.02
MAX_KILL_SWITCH_EVENTS_ALLOWED = 0


@dataclass(slots=True)
class LiveValidationScoreReport:
    total_attempts: int
    completed_trades: int  # BOTH_FILLED only — the only outcome with a real actual_net_pnl_usd to compare
    profitable_trades: int
    profitable_rate_pct: float | None
    mean_actual_net_pnl_usd: float | None
    mean_prediction_error_usd: float | None
    mean_abs_prediction_error_usd: float | None
    neutralization_failures: int
    unknown_leg_outcomes: int
    eligible_for_size_increase: bool
    eligibility_reason: str


async def _fetch_completed(session: AsyncSession) -> list[LiveArbitrageExecutionRecord]:
    stmt = select(LiveArbitrageExecutionRecord).where(LiveArbitrageExecutionRecord.outcome == "both_filled")
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def _count_by_outcome(session: AsyncSession, outcome: str) -> int:
    stmt = select(LiveArbitrageExecutionRecord).where(LiveArbitrageExecutionRecord.outcome == outcome)
    result = await session.execute(stmt)
    return len(list(result.scalars().all()))


async def build_live_validation_score(session: AsyncSession) -> LiveValidationScoreReport:
    total_stmt = select(LiveArbitrageExecutionRecord)
    total_attempts = len(list((await session.execute(total_stmt)).scalars().all()))

    completed = await _fetch_completed(session)
    neutralization_failures = await _count_by_outcome(session, "neutralization_failed")
    unknown_buy = await _count_by_outcome(session, "unknown_buy_leg")
    unknown_sell = await _count_by_outcome(session, "unknown_sell_leg")
    unknown_leg_outcomes = unknown_buy + unknown_sell

    return compute_validation_score(total_attempts, completed, neutralization_failures, unknown_leg_outcomes)


def compute_validation_score(
    total_attempts: int,
    completed: list[LiveArbitrageExecutionRecord],
    neutralization_failures: int,
    unknown_leg_outcomes: int,
) -> LiveValidationScoreReport:
    """Pure computation, split out from build_live_validation_score so it
    is unit-testable without a real database session — the same
    discipline as every other reporting module in this repo."""
    if not completed:
        return LiveValidationScoreReport(
            total_attempts=total_attempts,
            completed_trades=0,
            profitable_trades=0,
            profitable_rate_pct=None,
            mean_actual_net_pnl_usd=None,
            mean_prediction_error_usd=None,
            mean_abs_prediction_error_usd=None,
            neutralization_failures=neutralization_failures,
            unknown_leg_outcomes=unknown_leg_outcomes,
            eligible_for_size_increase=False,
            eligibility_reason="no completed (both_filled) real trades yet",
        )

    actual_pnls = [float(r.actual_net_pnl_usd) for r in completed if r.actual_net_pnl_usd is not None]
    prediction_errors = [float(r.prediction_error_usd) for r in completed if r.prediction_error_usd is not None]
    profitable = [p for p in actual_pnls if p > 0]
    profitable_rate = len(profitable) / len(actual_pnls) * 100 if actual_pnls else 0.0
    mean_pnl = statistics.fmean(actual_pnls) if actual_pnls else None
    mean_error = statistics.fmean(prediction_errors) if prediction_errors else None
    mean_abs_error = statistics.fmean(abs(e) for e in prediction_errors) if prediction_errors else None

    reasons = []
    eligible = True
    if len(completed) < MIN_COMPLETED_TRADES_TO_JUDGE:
        eligible = False
        reasons.append(f"only {len(completed)} completed trades (need >= {MIN_COMPLETED_TRADES_TO_JUDGE})")
    if profitable_rate < MIN_PROFITABLE_RATE_PCT:
        eligible = False
        reasons.append(f"profitable rate {profitable_rate:.1f}% (need >= {MIN_PROFITABLE_RATE_PCT:.0f}%)")
    if mean_abs_error is not None and mean_abs_error > MAX_MEAN_ABS_PREDICTION_ERROR_USD:
        eligible = False
        reasons.append(f"mean |prediction error| ${mean_abs_error:.4f} (need <= ${MAX_MEAN_ABS_PREDICTION_ERROR_USD})")
    if neutralization_failures > MAX_KILL_SWITCH_EVENTS_ALLOWED:
        eligible = False
        reasons.append(f"{neutralization_failures} neutralization failure(s) recorded")
    if unknown_leg_outcomes > MAX_KILL_SWITCH_EVENTS_ALLOWED:
        eligible = False
        reasons.append(f"{unknown_leg_outcomes} unknown-leg-outcome event(s) recorded")

    return LiveValidationScoreReport(
        total_attempts=total_attempts,
        completed_trades=len(completed),
        profitable_trades=len(profitable),
        profitable_rate_pct=profitable_rate,
        mean_actual_net_pnl_usd=mean_pnl,
        mean_prediction_error_usd=mean_error,
        mean_abs_prediction_error_usd=mean_abs_error,
        neutralization_failures=neutralization_failures,
        unknown_leg_outcomes=unknown_leg_outcomes,
        eligible_for_size_increase=eligible,
        eligibility_reason="all thresholds met" if eligible else "; ".join(reasons),
    )
