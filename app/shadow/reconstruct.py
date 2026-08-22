"""Shadow DB access — Phase 2, SHADOW MODE ONLY.

The ONLY module in app/shadow that touches the database. Reads
opportunities/simulated_trades/dex_simulated_trades (the real engines'
own already-persisted output — never mutated here) to build
ShadowOpportunitySummary and reconstruct OldEngineDecision, and writes
exclusively to shadow_decisions (never to any table a real engine reads
from). See app/shadow/__init__.py for the full isolation guarantee this
module is part of proving.
"""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import DexSimulatedTradeRecord, OpportunityRecord, ShadowDecisionRecord, SimulatedTradeRecord, VirtualPortfolioRecord
from app.shadow.ledger import ShadowCapitalLedger
from app.shadow.models import Engine, MasterDecision, MasterOutcome, OldEngineDecision, OldEngineOutcome, ShadowDecisionResult, ShadowOpportunitySummary

CEX_STRATEGIES = ("stablecoin", "cross_exchange", "triangular", "funding", "basis")
DEX_ATTEMPTABLE_STRATEGIES = ("dex_cross", "dex_triangular", "dex_multihop", "atomic")
CEX_REFERENCE_PORTFOLIO = "5K"  # the same portfolio the Phase 1 unification decision paired with DEX's $5,000 pool
BATCH_SIZE = 200


async def fetch_unevaluated_opportunities(session: AsyncSession, since: datetime, limit: int = BATCH_SIZE) -> list[ShadowOpportunitySummary]:
    """Every CEX or DEX opportunity detected since `since` that doesn't
    already have a shadow_decisions row — evaluated in detected_at order
    so the shadow ledger's capital contention plays out chronologically,
    matching how the real engines actually saw it arrive.

    PRE-PHASE-2 CORRECTIVE MAINTENANCE #2: an atomic opportunity and its
    sequential sibling share the EXACT same detected_at timestamp (see
    app.shadow.dedup) — if a hard LIMIT boundary happened to fall between
    two rows sharing that timestamp, the dedup grouping in the caller
    would only ever see one half of the pair in a given batch. Trims any
    trailing rows that share the last row's detected_at when the batch
    came back full, leaving them for the next poll instead of risking a
    split group — a small, deliberate under-fetch, not a correctness gap.
    """
    already_evaluated = select(ShadowDecisionRecord.opportunity_id)
    rows = (
        await session.execute(
            select(OpportunityRecord)
            .where(
                OpportunityRecord.strategy.in_(CEX_STRATEGIES + DEX_ATTEMPTABLE_STRATEGIES),
                OpportunityRecord.detected_at >= since,
                OpportunityRecord.id.not_in(already_evaluated),
            )
            .order_by(OpportunityRecord.detected_at.asc())
            .limit(limit)
        )
    ).scalars().all()

    if len(rows) == limit:
        boundary_ts = rows[-1].detected_at
        trimmed = list(rows)
        while trimmed and trimmed[-1].detected_at == boundary_ts:
            trimmed.pop()
        if trimmed:  # never fully empty the batch (would stall the orchestrator on a pathological all-same-timestamp batch)
            rows = trimmed

    summaries = []
    for row in rows:
        engine = Engine.DEX if row.strategy in DEX_ATTEMPTABLE_STRATEGIES else Engine.CEX
        chain = row.legs[0].get("chain") if row.legs and engine == Engine.DEX else None
        summaries.append(
            ShadowOpportunitySummary(
                opportunity_id=row.id,
                engine=engine,
                strategy=row.strategy,
                symbol=row.symbol,
                legs=row.legs or [],
                chain=chain,
                expected_profit_usd=float(row.expected_profit_usd) if row.expected_profit_usd is not None else None,
                capital_usd=float(row.capital_usd) if row.capital_usd is not None else None,
                execution_fill_probability=float(row.execution_fill_probability) if row.execution_fill_probability is not None else None,
                capital_velocity_score=float(row.capital_velocity_score) if row.capital_velocity_score is not None else None,
                holding_period_seconds=float(row.holding_period_seconds) if row.holding_period_seconds is not None else None,
                detected_at=row.detected_at,
                detection_time_rejection_reason=row.rejection_reason,
            )
        )
    return summaries


async def reconstruct_old_engine_decision(session: AsyncSession, opp: ShadowOpportunitySummary) -> OldEngineDecision:
    """What did the REAL engine actually do with this opportunity? Read-
    only — queries simulated_trades (CEX, "5K" reference portfolio only,
    matching the real capital this whole comparison is scaled against) or
    dex_simulated_trades (DEX), taking the EARLIEST trade row for this
    opportunity_id (a fair apples-to-apples comparison to Master's own
    one-time evaluation at detection — CEX's real loop can attempt a
    persisting opportunity many times across continuations; only the
    first attempt is what "the engine's decision on this opportunity"
    meant at the moment Master would have seen it too)."""
    if opp.detection_time_rejection_reason is not None:
        return OldEngineDecision(
            outcome=OldEngineOutcome.NOT_ATTEMPTED_REJECTED,
            reason=opp.detection_time_rejection_reason,
            net_profit_usd=None,
            capital_usd=None,
        )

    if opp.engine == Engine.CEX:
        row = (
            await session.execute(
                select(SimulatedTradeRecord.status, SimulatedTradeRecord.net_profit_usd, SimulatedTradeRecord.capital_usd)
                .select_from(SimulatedTradeRecord)
                .join(VirtualPortfolioRecord, VirtualPortfolioRecord.id == SimulatedTradeRecord.portfolio_id)
                .where(SimulatedTradeRecord.opportunity_id == opp.opportunity_id, VirtualPortfolioRecord.name == CEX_REFERENCE_PORTFOLIO)
                .order_by(SimulatedTradeRecord.executed_at.asc())
                .limit(1)
            )
        ).first()
        filled_statuses = ("simulated_executed", "partial_fill")
    else:
        row = (
            await session.execute(
                select(DexSimulatedTradeRecord.status, DexSimulatedTradeRecord.net_profit_usd, DexSimulatedTradeRecord.capital_usd)
                .where(DexSimulatedTradeRecord.opportunity_id == opp.opportunity_id)
                .order_by(DexSimulatedTradeRecord.execution_complete_at.asc())
                .limit(1)
            )
        ).first()
        filled_statuses = ("dex_filled",)

    if row is None:
        return OldEngineDecision(outcome=OldEngineOutcome.NOT_ATTEMPTED_NO_RECORD, reason=None, net_profit_usd=None, capital_usd=None)

    status, net_profit_usd, capital_usd = row
    outcome = OldEngineOutcome.FILLED if status in filled_statuses else OldEngineOutcome.ATTEMPTED_NOT_FILLED
    return OldEngineDecision(
        outcome=outcome,
        reason=status,
        net_profit_usd=float(net_profit_usd) if net_profit_usd is not None else None,
        capital_usd=float(capital_usd) if capital_usd is not None else None,
    )


def old_engine_approved(old: OldEngineDecision) -> bool:
    return old.outcome in (OldEngineOutcome.FILLED, OldEngineOutcome.ATTEMPTED_NOT_FILLED)


def master_approved(master: MasterDecision) -> bool:
    return master.outcome == MasterOutcome.ALLOCATE


async def persist_shadow_decision(session: AsyncSession, result: ShadowDecisionResult) -> None:
    """INSERT ... ON CONFLICT DO NOTHING on opportunity_id — each
    opportunity is evaluated exactly once, ever; a re-run of the
    orchestrator (e.g. after a restart) must never overwrite an already-
    recorded comparison with a re-evaluation against a since-changed
    shadow ledger state."""
    stmt = pg_insert(ShadowDecisionRecord).values(
        opportunity_id=result.opportunity.opportunity_id,
        engine=result.opportunity.engine.value,
        strategy=result.opportunity.strategy,
        symbol=result.opportunity.symbol,
        detected_at=result.opportunity.detected_at,
        decided_at=result.decided_at,
        old_engine_outcome=result.old.outcome.value,
        old_engine_reason=result.old.reason,
        old_engine_net_profit_usd=result.old.net_profit_usd,
        old_engine_capital_usd=result.old.capital_usd,
        master_outcome=result.master.outcome.value,
        master_reason=result.master.reason,
        master_rank_score=result.master.rank_score,
        master_capital_reserved_usd=result.master.theoretical_capital_reserved_usd,
        master_projected_net_profit_usd=result.master.projected_net_profit_usd,
        master_capital_available_global_usd=result.master.capital_available_global_usd,
        agree=result.agree,
    ).on_conflict_do_nothing(index_elements=["opportunity_id"])
    await session.execute(stmt)


async def rebuild_shadow_ledger_from_history(session: AsyncSession) -> ShadowCapitalLedger:
    """Same "trust the persisted ledger over any live in-memory number"
    invariant already established for the real CEX/DEX capital state
    (app.simulation.state_recovery, app.reporting.global_capital) —
    applied here so a shadow_orchestrator.py restart doesn't silently
    reset cumulative theoretical P&L back to zero. Reservations are NOT
    replayed (every real opportunity's holding period is seconds to
    minutes — by the time any restart happens, they've all already
    expired), only realized_pnl_usd is restored."""
    realized_pnl = (
        await session.execute(
            select(func.coalesce(func.sum(ShadowDecisionRecord.master_projected_net_profit_usd), 0)).where(
                ShadowDecisionRecord.master_outcome == MasterOutcome.ALLOCATE.value
            )
        )
    ).scalar() or 0.0
    return ShadowCapitalLedger(realized_pnl_usd=float(realized_pnl))
