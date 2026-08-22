"""PHASE 2B — CEX Scan-Level Shadow reporting (user directive,
2026-08-22).

Pure read-side aggregation over cex_scan_shadow_decisions (never a write
path) — reports NEW DETECTION / CONTINUATION / GLOBAL agreement
separately, per spec's explicit "ne mélange plus nouvelles détections et
continuations invisibles."
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import CexScanShadowDecisionRecord


@dataclass(slots=True)
class CexScanAgreementBreakdown:
    hours: float
    new_detection_total: int
    new_detection_agree: int
    new_detection_agreement_pct: float | None
    continuation_total: int
    continuation_agree: int
    continuation_agreement_pct: float | None
    global_total: int
    global_agree: int
    global_agreement_pct: float | None
    old_accepted_master_rejected: int
    master_accepted_old_rejected: int


async def build_cex_scan_agreement_breakdown(session: AsyncSession, hours: float = 24.0, now: datetime | None = None) -> CexScanAgreementBreakdown:
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)

    new_case = case((CexScanShadowDecisionRecord.is_new_detection.is_(True), 1), else_=0)
    cont_case = case((CexScanShadowDecisionRecord.is_new_detection.is_(False), 1), else_=0)
    new_agree_case = case((CexScanShadowDecisionRecord.is_new_detection.is_(True) & CexScanShadowDecisionRecord.agree, 1), else_=0)
    cont_agree_case = case((CexScanShadowDecisionRecord.is_new_detection.is_(False) & CexScanShadowDecisionRecord.agree, 1), else_=0)
    agree_case = case((CexScanShadowDecisionRecord.agree.is_(True), 1), else_=0)
    old_accepted_master_rejected_case = case(
        ((CexScanShadowDecisionRecord.old_approved.is_(True)) & (CexScanShadowDecisionRecord.master_outcome != "allocate"), 1), else_=0
    )
    master_accepted_old_rejected_case = case(
        ((CexScanShadowDecisionRecord.old_approved.is_(False)) & (CexScanShadowDecisionRecord.master_outcome == "allocate"), 1), else_=0
    )

    row = (
        await session.execute(
            select(
                func.count(),
                func.coalesce(func.sum(new_case), 0),
                func.coalesce(func.sum(new_agree_case), 0),
                func.coalesce(func.sum(cont_case), 0),
                func.coalesce(func.sum(cont_agree_case), 0),
                func.coalesce(func.sum(agree_case), 0),
                func.coalesce(func.sum(old_accepted_master_rejected_case), 0),
                func.coalesce(func.sum(master_accepted_old_rejected_case), 0),
            ).where(CexScanShadowDecisionRecord.decided_at >= cutoff)
        )
    ).first()

    total, new_total, new_agree, cont_total, cont_agree, global_agree, oam_rejected, mao_accepted = (int(x or 0) for x in row)

    return CexScanAgreementBreakdown(
        hours=hours,
        new_detection_total=new_total,
        new_detection_agree=new_agree,
        new_detection_agreement_pct=(new_agree / new_total * 100) if new_total else None,
        continuation_total=cont_total,
        continuation_agree=cont_agree,
        continuation_agreement_pct=(cont_agree / cont_total * 100) if cont_total else None,
        global_total=total,
        global_agree=global_agree,
        global_agreement_pct=(global_agree / total * 100) if total else None,
        old_accepted_master_rejected=oam_rejected,
        master_accepted_old_rejected=mao_accepted,
    )


@dataclass(slots=True)
class CexScanDisagreementRow:
    old_approved: bool
    old_rejection_reason: str | None
    master_outcome: str
    master_reason: str | None
    count: int


async def build_cex_scan_disagreement_breakdown(
    session: AsyncSession, hours: float = 24.0, now: datetime | None = None, limit: int = 20
) -> list[CexScanDisagreementRow]:
    """Groups every disagreement by its (old_rejection_reason,
    master_outcome) pattern — the raw material for manually classifying
    each cause into the 5 categories spec asks for; not itself an
    automated classifier, since "intentional ranking difference" vs
    "timing difference" genuinely requires judgment a SQL GROUP BY can't
    make. master_reason is deliberately NOT part of the GROUP BY — it
    embeds per-row detail (a specific position key, a specific EV value)
    that would fragment this into one row per unique detail instead of a
    meaningful aggregate; one representative sample is returned per group
    via MIN() instead."""
    now = now or datetime.now(UTC).replace(tzinfo=None)
    cutoff = now - timedelta(hours=hours)
    rows = (
        await session.execute(
            select(
                CexScanShadowDecisionRecord.old_approved,
                CexScanShadowDecisionRecord.old_rejection_reason,
                CexScanShadowDecisionRecord.master_outcome,
                func.min(CexScanShadowDecisionRecord.master_reason),
                func.count(),
            )
            .where(CexScanShadowDecisionRecord.decided_at >= cutoff, CexScanShadowDecisionRecord.agree.is_(False))
            .group_by(
                CexScanShadowDecisionRecord.old_approved,
                CexScanShadowDecisionRecord.old_rejection_reason,
                CexScanShadowDecisionRecord.master_outcome,
            )
            .order_by(func.count().desc())
            .limit(limit)
        )
    ).all()
    return [
        CexScanDisagreementRow(old_approved=r[0], old_rejection_reason=r[1], master_outcome=r[2], master_reason=r[3], count=int(r[4]))
        for r in rows
    ]
