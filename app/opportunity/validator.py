"""Opportunity Classification (section 16) — thresholds on net spread %.

Starting thresholds from the cahier des charges; expected to be recalibrated
after the 7-day observation window (section 16 note).
"""

from app.config.constants import CLASSIFICATION_THRESHOLDS, OpportunityClassification

# Ordered worst -> best so the first threshold the spread clears (from the top) wins.
_ORDERED = sorted(CLASSIFICATION_THRESHOLDS.items(), key=lambda kv: kv[1], reverse=True)


def classify(net_spread_pct: float) -> OpportunityClassification:
    if net_spread_pct < 0:
        return OpportunityClassification.NOT_PROFITABLE
    for classification, threshold in _ORDERED:
        if net_spread_pct >= threshold:
            return classification
    return OpportunityClassification.WATCH
