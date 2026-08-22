"""Phase 2 — Global Orchestration, SHADOW MODE ONLY reporting (user
directive, 2026-08-22).

Pure read-side aggregation over shadow_decisions — the table
shadow_orchestrator.py (a separate process, see app/shadow/__init__.py)
writes to. This module only ever SELECTs; it has no write path at all.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ShadowDecisionRecord


@dataclass(slots=True)
class ShadowSummary:
    hours: float
    total_decisions: int
    agree_count: int
    disagree_count: int
    agreement_pct: float | None
    old_approved_master_rejected: int  # OLD accepted, MASTER would have rejected
    old_rejected_master_approved: int  # OLD rejected/didn't attempt, MASTER would have allocated
    capital_conflicts_detected: int  # MASTER rejections specifically for lack of capital (NO the opportunity wasn't bad, capital was the binding constraint)
    double_allocations_prevented: int  # same as capital_conflicts_detected, from the allocator's own perspective — surfaced under both names per spec
    theoretical_capital_reserved_usd: float
    old_pnl_usd: float
    master_pnl_usd: float
    pnl_difference_usd: float


@dataclass(slots=True)
class ShadowEngineBreakdown:
    engine: str
    total_decisions: int
    agreement_pct: float | None
    old_pnl_usd: float
    master_pnl_usd: float


@dataclass(slots=True)
class ShadowStrategyBreakdown:
    strategy: str
    engine: str
    total_decisions: int
    agreement_pct: float | None
    old_pnl_usd: float
    master_pnl_usd: float


@dataclass(slots=True)
class ShadowRecentDecision:
    opportunity_id: str
    engine: str
    strategy: str
    symbol: str
    old_outcome: str
    master_outcome: str
    agree: bool
    master_rank_score: float
    decided_at: datetime


async def build_shadow_summary(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> ShadowSummary:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    agree_case = case((ShadowDecisionRecord.agree.is_(True), 1), else_=0)
    old_approved_master_rejected_case = case(
        (
            (ShadowDecisionRecord.old_engine_outcome.in_(["filled", "attempted_not_filled"])) & (ShadowDecisionRecord.master_outcome != "allocate"),
            1,
        ),
        else_=0,
    )
    old_rejected_master_approved_case = case(
        (
            (ShadowDecisionRecord.old_engine_outcome.in_(["not_attempted_rejected", "not_attempted_no_record"]))
            & (ShadowDecisionRecord.master_outcome == "allocate"),
            1,
        ),
        else_=0,
    )
    capital_conflict_case = case((ShadowDecisionRecord.master_outcome == "reject_no_capital", 1), else_=0)

    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(agree_case), 0),
                func.coalesce(func.sum(old_approved_master_rejected_case), 0),
                func.coalesce(func.sum(old_rejected_master_approved_case), 0),
                func.coalesce(func.sum(capital_conflict_case), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.master_capital_reserved_usd), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.old_engine_net_profit_usd), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.master_projected_net_profit_usd), 0),
            ).where(ShadowDecisionRecord.decided_at >= cutoff)
        )
    ).first()

    total, agree_count, old_approved_master_rejected, old_rejected_master_approved, capital_conflicts, capital_reserved, old_pnl, master_pnl = (
        int(row[0] or 0),
        int(row[1] or 0),
        int(row[2] or 0),
        int(row[3] or 0),
        int(row[4] or 0),
        float(row[5] or 0.0),
        float(row[6] or 0.0),
        float(row[7] or 0.0),
    )

    return ShadowSummary(
        hours=hours,
        total_decisions=total,
        agree_count=agree_count,
        disagree_count=total - agree_count,
        agreement_pct=(agree_count / total * 100) if total else None,
        old_approved_master_rejected=old_approved_master_rejected,
        old_rejected_master_approved=old_rejected_master_approved,
        capital_conflicts_detected=capital_conflicts,
        double_allocations_prevented=capital_conflicts,
        theoretical_capital_reserved_usd=capital_reserved,
        old_pnl_usd=old_pnl,
        master_pnl_usd=master_pnl,
        pnl_difference_usd=master_pnl - old_pnl,
    )


async def build_shadow_engine_breakdown(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> list[ShadowEngineBreakdown]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)
    agree_case = case((ShadowDecisionRecord.agree.is_(True), 1), else_=0)
    rows = (
        await session.execute(
            select(
                ShadowDecisionRecord.engine,
                func.count(),
                func.coalesce(func.sum(agree_case), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.old_engine_net_profit_usd), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.master_projected_net_profit_usd), 0),
            )
            .where(ShadowDecisionRecord.decided_at >= cutoff)
            .group_by(ShadowDecisionRecord.engine)
        )
    ).all()
    return [
        ShadowEngineBreakdown(
            engine=engine,
            total_decisions=int(total),
            agreement_pct=(int(agree) / int(total) * 100) if total else None,
            old_pnl_usd=float(old_pnl),
            master_pnl_usd=float(master_pnl),
        )
        for engine, total, agree, old_pnl, master_pnl in rows
    ]


async def build_shadow_strategy_breakdown(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> list[ShadowStrategyBreakdown]:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)
    agree_case = case((ShadowDecisionRecord.agree.is_(True), 1), else_=0)
    rows = (
        await session.execute(
            select(
                ShadowDecisionRecord.strategy,
                ShadowDecisionRecord.engine,
                func.count(),
                func.coalesce(func.sum(agree_case), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.old_engine_net_profit_usd), 0),
                func.coalesce(func.sum(ShadowDecisionRecord.master_projected_net_profit_usd), 0),
            )
            .where(ShadowDecisionRecord.decided_at >= cutoff)
            .group_by(ShadowDecisionRecord.strategy, ShadowDecisionRecord.engine)
            .order_by(func.count().desc())
        )
    ).all()
    return [
        ShadowStrategyBreakdown(
            strategy=strategy,
            engine=engine,
            total_decisions=int(total),
            agreement_pct=(int(agree) / int(total) * 100) if total else None,
            old_pnl_usd=float(old_pnl),
            master_pnl_usd=float(master_pnl),
        )
        for strategy, engine, total, agree, old_pnl, master_pnl in rows
    ]


async def list_recent_shadow_decisions(session: AsyncSession, limit: int = 15) -> list[ShadowRecentDecision]:
    rows = (
        await session.execute(
            select(ShadowDecisionRecord).order_by(ShadowDecisionRecord.decided_at.desc()).limit(limit)
        )
    ).scalars().all()
    return [
        ShadowRecentDecision(
            opportunity_id=str(r.opportunity_id),
            engine=r.engine,
            strategy=r.strategy,
            symbol=r.symbol,
            old_outcome=r.old_engine_outcome,
            master_outcome=r.master_outcome,
            agree=r.agree,
            master_rank_score=float(r.master_rank_score),
            decided_at=r.decided_at,
        )
        for r in rows
    ]
