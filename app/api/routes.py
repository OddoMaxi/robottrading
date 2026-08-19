from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OpportunityRecord
from app.database.session import get_session

router = APIRouter()


@router.get("/health")
async def health() -> dict:
    return {"status": "ok"}


@router.get("/opportunities")
async def list_opportunities(limit: int = 50, session: AsyncSession = Depends(get_session)) -> list[dict]:
    result = await session.execute(
        select(OpportunityRecord).order_by(OpportunityRecord.detected_at.desc()).limit(limit)
    )
    return [
        {
            "id": str(row.id),
            "strategy": row.strategy,
            "symbol": row.symbol,
            "gross_spread_pct": row.gross_spread_pct,
            "net_spread_pct": row.net_spread_pct,
            "break_even_pct": row.break_even_pct,
            "score": row.score,
            "classification": row.classification,
            "detected_at": row.detected_at.isoformat() if row.detected_at else None,
        }
        for row in result.scalars()
    ]
