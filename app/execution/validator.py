"""Execution Validator (Continuous Execution spec, sections 12-15, 43).

The single gate every opportunity passes through before any capital gets
allocated to it. Replaces the previously-scattered ad-hoc checks
(classification threshold, position-already-open) with one function that
always returns a traceable reason — APPROVED, or REJECTED with a specific
code — so the dashboard's rejection-reason breakdown (section 43) has
something real to show instead of a silent "nothing happened."

Capital availability and risk-limit sizing stay out of this gate on
purpose: those are per-portfolio (5 virtual portfolios can each have a
different answer for the same opportunity), while this validator answers
one global, portfolio-independent question — "is this opportunity even
worth attempting." PaperTrader.simulate already reports
NO_CAPITAL_AVAILABLE per portfolio for the capital-specific case.
"""

import time
from dataclasses import dataclass
from enum import StrEnum

from app.config.constants import OpportunityClassification
from app.opportunity.false_opportunity_filter import MAX_QUOTE_AGE_SECONDS
from app.opportunity.models import Opportunity
from app.simulation.position_tracker import OpenPositionTracker

# Net-positive but below the "worth attempting" bar — a real edge, just too
# small once execution risk is weighed. Distinct from FEES_TOO_HIGH, where
# there was no edge left *at all* once fees were paid.
_MARGINAL_CLASSIFICATIONS = {OpportunityClassification.WATCH}


class RejectionReason(StrEnum):
    STALE_DATA = "stale_data"
    FEES_TOO_HIGH = "fees_too_high"  # net_spread_pct <= 0 — the whole gross edge went to fees/costs
    EDGE_TOO_LOW = "edge_too_low"  # net-positive but below the minimum worth attempting
    POSITION_ALREADY_OPEN = "position_already_open"


@dataclass(slots=True)
class ValidationResult:
    approved: bool
    reason: RejectionReason | None = None


def validate(opp: Opportunity, position_tracker: OpenPositionTracker, now: float | None = None) -> ValidationResult:
    now = now if now is not None else time.time()

    if opp.market_data_age_seconds is not None and opp.market_data_age_seconds > MAX_QUOTE_AGE_SECONDS:
        return ValidationResult(False, RejectionReason.STALE_DATA)

    if opp.classification == OpportunityClassification.NOT_PROFITABLE:
        return ValidationResult(False, RejectionReason.FEES_TOO_HIGH)
    if opp.classification is None or opp.classification in _MARGINAL_CLASSIFICATIONS:
        return ValidationResult(False, RejectionReason.EDGE_TOO_LOW)

    if opp.holding_period_seconds is not None and opp.legs:
        position_key = (opp.strategy, opp.legs[0].get("exchange"), opp.symbol)
        if position_tracker.is_open(position_key, now):
            return ValidationResult(False, RejectionReason.POSITION_ALREADY_OPEN)

    return ValidationResult(True, None)
