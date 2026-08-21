"""Portfolio Ledger integrity (Reality Engine spec, sections 30-31).

The one invariant that must never break in a no-leverage system:

    equity >= 0
    available_usd >= 0
    live in-memory equity == the DB trade ledger's own reconstruction of it

The first two are already enforced by construction — VirtualPortfolio.
available_usd clamps to >= 0, and VirtualPortfolio.lock_capital rejects
any reservation that would push it negative (urgent audit fix). This
module exists to *prove* that continuously rather than just trust it, and
to catch the one failure mode those guards can't: the live in-memory
portfolio silently drifting out of sync with the DB ledger (a write that
silently failed, a state-recovery inconsistency after a restart). If this
check ever fails, spec section 10's "STOP EXECUTION, log CRITICAL ERROR"
applies — main.py engages the kill switch on a violation.
"""

import logging
import time
from dataclasses import dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession

from app.reporting.simple_summary import build_portfolio_capital
from app.simulation.portfolios import VirtualPortfolio

logger = logging.getLogger(__name__)

# A cent of drift is meaningful here — nothing in this system's arithmetic
# legitimately produces floating-point error anywhere near this scale, so
# anything above it means the two sources of truth actually disagree.
DEFAULT_TOLERANCE_USD = 0.01


@dataclass(slots=True)
class LedgerIntegrityCheck:
    portfolio_name: str
    equity_usd: float
    available_usd: float
    db_reconstructed_equity_usd: float
    reconciled: bool
    violations: list[str] = field(default_factory=list)


def evaluate_ledger_integrity(
    portfolio_name: str,
    live_equity_usd: float,
    live_available_usd: float,
    db_reconstructed_equity_usd: float,
    tolerance_usd: float = DEFAULT_TOLERANCE_USD,
) -> LedgerIntegrityCheck:
    """Pure comparison step, split out from the DB round-trip below so the
    actual invariant logic is unit-testable without a database."""
    violations: list[str] = []
    if live_available_usd < -tolerance_usd:
        violations.append(f"available_usd is negative: {live_available_usd:.6f}")
    if live_equity_usd < -tolerance_usd:
        violations.append(f"equity is negative: {live_equity_usd:.2f}")
    discrepancy = live_equity_usd - db_reconstructed_equity_usd
    if abs(discrepancy) > tolerance_usd:
        violations.append(
            f"live equity ({live_equity_usd:.2f}) disagrees with the DB ledger's reconstruction "
            f"({db_reconstructed_equity_usd:.2f}) by {discrepancy:+.2f}"
        )
    return LedgerIntegrityCheck(
        portfolio_name=portfolio_name,
        equity_usd=live_equity_usd,
        available_usd=live_available_usd,
        db_reconstructed_equity_usd=db_reconstructed_equity_usd,
        reconciled=not violations,
        violations=violations,
    )


async def check_ledger_integrity(
    session: AsyncSession, portfolio: VirtualPortfolio, portfolio_id: int, now: float | None = None
) -> LedgerIntegrityCheck:
    now = now if now is not None else time.time()
    db_equity = await build_portfolio_capital(session, portfolio_id, portfolio.initial_capital_usd)
    return evaluate_ledger_integrity(
        portfolio.name, portfolio.current_value_usd, portfolio.available_usd(now), db_equity
    )
