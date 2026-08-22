"""Shadow data shapes (Phase 2, SHADOW MODE ONLY).

Deliberately flat, standalone dataclasses populated directly from raw DB
rows (app.shadow.reconstruct) rather than reusing app.opportunity.models.
Opportunity or app.database.models.OpportunityRecord — keeps this
package's dependency graph minimal and self-evidently isolated from the
real engines (see app/shadow/__init__.py).
"""

import uuid
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Engine(StrEnum):
    CEX = "CEX"
    DEX = "DEX"


@dataclass(slots=True)
class ShadowOpportunitySummary:
    """The minimal read-only snapshot of an already-detected, already-
    persisted opportunity that the Master Ranker needs — nothing here is
    computed by this package, it's all copied straight from the real
    engines' own detection-time output (app.database.models.
    OpportunityRecord), never re-derived or guessed."""

    opportunity_id: uuid.UUID
    engine: Engine
    strategy: str
    symbol: str
    legs: list[dict]  # PRE-PHASE-2 CORRECTIVE MAINTENANCE #2: needed for both economic-event dedup (app.shadow.dedup) and CEX position-key derivation (app.shadow.positions) — copied verbatim from OpportunityRecord.legs, never mutated
    chain: str | None  # DEX only, from legs[0].chain
    expected_profit_usd: float | None
    capital_usd: float | None
    execution_fill_probability: float | None
    capital_velocity_score: float | None  # 0-100, already-computed by the real detection engines
    holding_period_seconds: float | None
    detected_at: datetime
    detection_time_rejection_reason: str | None  # e.g. "duplicate_economic_event" — set by the REAL engine at detection time


class OldEngineOutcome(StrEnum):
    FILLED = "filled"
    ATTEMPTED_NOT_FILLED = "attempted_not_filled"
    NOT_ATTEMPTED_REJECTED = "not_attempted_rejected"  # rejection_reason was set at detection (e.g. duplicate_economic_event)
    NOT_ATTEMPTED_NO_RECORD = "not_attempted_no_record"  # no rejection reason, but no trade record found either


@dataclass(slots=True)
class OldEngineDecision:
    outcome: OldEngineOutcome
    reason: str | None
    net_profit_usd: float | None  # None unless outcome == FILLED (or a genuine loss on ATTEMPTED_NOT_FILLED)
    capital_usd: float | None


class MasterOutcome(StrEnum):
    ALLOCATE = "allocate"
    REJECT_NO_CAPITAL = "reject_no_capital"
    REJECT_NOT_PROFITABLE = "reject_not_profitable"
    REJECT_MISSING_DATA = "reject_missing_data"
    # PRE-PHASE-2 CORRECTIVE MAINTENANCE #2 (2026-08-22): the two fixes
    # this session adds — see app.shadow.dedup and app.shadow.positions.
    REJECT_DUPLICATE_ECONOMIC_EVENT = "reject_duplicate_economic_event"
    REJECT_POSITION_ALREADY_OPEN = "reject_position_already_open"


@dataclass(slots=True)
class MasterDecision:
    outcome: MasterOutcome
    reason: str | None
    rank_score: float
    theoretical_capital_reserved_usd: float | None
    projected_net_profit_usd: float | None
    capital_available_global_usd: float  # snapshot of the shadow ledger AT the decision instant


@dataclass(slots=True)
class ShadowDecisionResult:
    opportunity: ShadowOpportunitySummary
    old: OldEngineDecision
    master: MasterDecision
    agree: bool
    decided_at: datetime
