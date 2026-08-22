"""Global Capital State (V5/V5.5 Master Orchestration, user directive,
2026-08-22, spec Parts F/O/P).

READ-SIDE reconciliation, not a write-path allocator: this reconstructs
the unified capital picture from the two engines' own authoritative
ledgers (app.reporting.simple_summary's CEX reconstruction, and the DEX
equivalent below) — the same principle app.simulation.ledger_integrity
already uses for CEX alone (trust the persisted trade ledger over any
live in-memory number), applied across both engines. Deliberately does
NOT yet route real reservations through a shared allocator that CEX/DEX
executors call into — spec Part AM requires shadow validation before that
cutover, and the CEX vs DEX capital models are architecturally different
enough (see below) that cutting over before validating would be reckless.

ARCHITECTURE FINDING (this audit): CEX runs FIVE parallel what-if
portfolios ("500"/"1K"/"5K"/"10K"/"25K") that each replay the SAME
detected opportunities at different capital scales for comparison — they
are not five slices of one real budget. DEX runs exactly ONE real capital
pool. Per explicit user decision (2026-08-22), the CEX "5K" portfolio
(app.reporting.rotation.ROTATION_REFERENCE_PORTFOLIO's own convention)
is the one paired with DEX's $5,000 pool for a unified $10,000 view; the
other four CEX portfolios remain separate what-if instrumentation,
excluded from this unified ledger.

DEX_PAPER_TRADING_CAPITAL_USD below intentionally duplicates main.py's
own constant of the same value rather than importing it — importing from
main.py (the live engine entrypoint) from a reporting module risks
import-time side effects and couples the dashboard's read path to the
engine process; consolidating into one shared constant is deferred to the
next engine restart (see the audit's final report, section Q).
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DexSimulatedTradeRecord
from app.reporting.simple_summary import CapitalUtilization, build_capital_utilization, build_portfolio_capital

DEX_PAPER_TRADING_CAPITAL_USD = 5_000.0  # mirrors main.py's own constant — see module docstring
DEX_FILLED_STATUS = "dex_filled"


@dataclass(slots=True)
class DexCapitalSnapshot:
    total_capital_usd: float
    realized_pnl_usd: float
    equity_usd: float  # total + realized_pnl
    locked_usd: float  # capital genuinely in-flight AT `now` (execution_attempt_at <= now < execution_complete_at)
    available_usd: float
    utilization_pct: float


async def build_dex_capital_snapshot(session: AsyncSession, total_capital_usd: float, now: datetime | None = None) -> DexCapitalSnapshot:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    realized_pnl = (
        await session.execute(select(func.coalesce(func.sum(DexSimulatedTradeRecord.net_profit_usd), 0)))
    ).scalar() or 0.0
    equity_usd = total_capital_usd + float(realized_pnl)

    locked = (
        await session.execute(
            select(func.coalesce(func.sum(DexSimulatedTradeRecord.capital_usd), 0)).where(
                DexSimulatedTradeRecord.status == DEX_FILLED_STATUS,
                DexSimulatedTradeRecord.execution_attempt_at <= now,
                DexSimulatedTradeRecord.execution_complete_at > now,
            )
        )
    ).scalar() or 0.0
    locked_usd = float(locked)
    available_usd = max(equity_usd - locked_usd, 0.0)

    return DexCapitalSnapshot(
        total_capital_usd=total_capital_usd,
        realized_pnl_usd=float(realized_pnl),
        equity_usd=equity_usd,
        locked_usd=locked_usd,
        available_usd=available_usd,
        utilization_pct=(locked_usd / equity_usd * 100) if equity_usd else 0.0,
    )


@dataclass(slots=True)
class GlobalCapitalState:
    total_capital_usd: float
    available_usd: float
    reserved_cex_usd: float  # CEX "engaged" capital — open positions, see simple_summary.build_capital_utilization
    reserved_dex_usd: float  # DEX capital genuinely in-flight at `now`
    total_reserved_usd: float
    capital_utilization_pct: float
    cex_total_capital_usd: float
    cex_available_usd: float
    dex_total_capital_usd: float
    dex_available_usd: float

    @property
    def reconciled(self) -> bool:
        """The one hard invariant Part F/AR demands: total == available +
        every reserved bucket, to within floating-point tolerance."""
        return abs(self.total_capital_usd - (self.available_usd + self.total_reserved_usd)) < 0.01


async def build_global_capital_state(
    session: AsyncSession,
    cex_portfolio_id: int,
    cex_initial_capital_usd: float,
    dex_total_capital_usd: float = DEX_PAPER_TRADING_CAPITAL_USD,
    now: datetime | None = None,
) -> GlobalCapitalState:
    now = now or datetime.now(UTC).replace(tzinfo=None)

    cex_equity_usd = await build_portfolio_capital(session, cex_portfolio_id, cex_initial_capital_usd)
    cex_util: CapitalUtilization = await build_capital_utilization(session, cex_portfolio_id, cex_equity_usd, now)
    cex_available_usd = max(cex_equity_usd - cex_util.engaged_usd, 0.0)

    dex_snapshot = await build_dex_capital_snapshot(session, dex_total_capital_usd, now)

    total_capital_usd = cex_equity_usd + dex_snapshot.equity_usd
    reserved_cex_usd = cex_util.engaged_usd
    reserved_dex_usd = dex_snapshot.locked_usd
    total_reserved_usd = reserved_cex_usd + reserved_dex_usd
    available_usd = cex_available_usd + dex_snapshot.available_usd

    return GlobalCapitalState(
        total_capital_usd=total_capital_usd,
        available_usd=available_usd,
        reserved_cex_usd=reserved_cex_usd,
        reserved_dex_usd=reserved_dex_usd,
        total_reserved_usd=total_reserved_usd,
        capital_utilization_pct=(total_reserved_usd / total_capital_usd * 100) if total_capital_usd else 0.0,
        cex_total_capital_usd=cex_equity_usd,
        cex_available_usd=cex_available_usd,
        dex_total_capital_usd=dex_snapshot.equity_usd,
        dex_available_usd=dex_snapshot.available_usd,
    )
