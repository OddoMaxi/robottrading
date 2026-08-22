"""Phase 2 — Global Orchestration, SHADOW MODE ONLY entrypoint (user
directive, 2026-08-22).

A completely separate process from main.py — does NOT import main, does
NOT import app.simulation.paper_trader, app.simulation.portfolios, or
app.onchain.dex_paper_trader (see app/shadow/__init__.py and
tests/test_shadow_isolation.py, which enforces this mechanically). This
process can be stopped, crashed, or deleted entirely without touching
V5/V5.5's real capital, positions, or decisions in any way — it only
reads their already-persisted historical output and writes to its own
shadow_decisions table.

Run with: python shadow_orchestrator.py
Deployed as its own systemd unit (robotcripto-shadow.service),
independent of robotcripto-engine.service.
"""

import asyncio
import logging
from datetime import UTC, datetime

from app.database.session import async_session_factory
from app.reporting.reality_baseline import REALITY_BASELINE_AT
from app.shadow.decision import evaluate_shadow_decision
from app.shadow.ledger import ShadowCapitalLedger
from app.shadow.models import ShadowDecisionResult
from app.shadow.reconstruct import (
    fetch_unevaluated_opportunities,
    master_approved,
    old_engine_approved,
    persist_shadow_decision,
    rebuild_shadow_ledger_from_history,
    reconstruct_old_engine_decision,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s:%(name)s:%(message)s")
logger = logging.getLogger("shadow_orchestrator")

POLL_INTERVAL_SECONDS = 15.0

# Only evaluate opportunities from Phase 1's own validated baseline
# onward — matches the same "never blend contaminated pre-fix data"
# discipline app.reporting.reality_baseline established.
SHADOW_EVALUATION_SINCE = REALITY_BASELINE_AT


async def run_one_batch(ledger: ShadowCapitalLedger) -> int:
    async with async_session_factory() as session:
        opportunities = await fetch_unevaluated_opportunities(session, SHADOW_EVALUATION_SINCE)
        if not opportunities:
            return 0

        for opp in opportunities:
            old = await reconstruct_old_engine_decision(session, opp)
            master = evaluate_shadow_decision(opp, ledger, opp.detected_at)
            agree = old_engine_approved(old) == master_approved(master)
            result = ShadowDecisionResult(
                opportunity=opp,
                old=old,
                master=master,
                agree=agree,
                decided_at=datetime.now(UTC).replace(tzinfo=None),
            )
            await persist_shadow_decision(session, result)

        await session.commit()
        return len(opportunities)


async def main() -> None:
    logger.info("shadow_orchestrator starting — SHADOW MODE ONLY, no executor ever called, real_orders_placed stays false")
    async with async_session_factory() as session:
        ledger = await rebuild_shadow_ledger_from_history(session)
    logger.info("shadow ledger rebuilt from history: realized_pnl_usd=%.4f", ledger.realized_pnl_usd)

    while True:
        try:
            processed = await run_one_batch(ledger)
            if processed:
                logger.info("evaluated %d opportunities", processed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("shadow_orchestrator batch failed — will retry next poll, real engines are unaffected")
        await asyncio.sleep(POLL_INTERVAL_SECONDS)


if __name__ == "__main__":
    asyncio.run(main())
