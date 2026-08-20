"""Opportunity Deduplication & Lifecycle Tracking (Continuous Execution
spec, sections 3, 5-11, 41).

A spread that persists for several seconds isn't N separate opportunities
— it's one opportunity with N observations. Detection is event-driven and
can re-fire many times a second, so without this, the exact same economic
event gets recorded as thousands of near-identical rows (this is why the
opportunities table has grown into the millions). This tracker recognizes
a freshly-detected Opportunity as either:

  - a continuation of one already being watched (same dedup key, still
    within the liveness window) — updates its running stats, no new DB row;
  - a genuinely new economic event (first sighting, or a re-emergence after
    the signal fully disappeared — spec section 11's "independent edge
    detection") — gets its own DB row.
"""

import time
from dataclasses import dataclass, field

from app.config.constants import OpportunityStatus
from app.opportunity.models import Opportunity

# How long a dedup key can go unobserved before the next sighting counts as
# a brand new opportunity rather than a continuation. Set with headroom
# above the detection loop's own scan cadence (event-driven, but bounded by
# MIN_SCAN_INTERVAL_SECONDS/MAX_SCAN_WAIT_SECONDS in main.py) so a single
# slow scan cycle doesn't wrongly split one opportunity into two.
DEFAULT_LIVENESS_SECONDS = 5.0


def opportunity_key(opp: Opportunity) -> str:
    """strategy|symbol|exchange_market_side+... — spec section 5's dedup key,
    built from fields every engine already sets deterministically per leg."""
    legs_desc = "+".join(f"{leg.get('exchange')}_{leg.get('market')}_{leg.get('side')}" for leg in opp.legs)
    return f"{opp.strategy}|{opp.symbol}|{legs_desc}"


@dataclass(slots=True)
class TrackedOpportunity:
    key: str
    opportunity_id: object  # the uuid.UUID of the DB row backing this tracked entry
    first_seen: float
    last_seen: float
    initial_edge_pct: float
    current_edge_pct: float
    max_edge_pct: float
    min_edge_pct: float
    updates_count: int
    status: OpportunityStatus
    _edge_sum: float = field(default=0.0, repr=False)

    @property
    def avg_edge_pct(self) -> float:
        return self._edge_sum / self.updates_count if self.updates_count else 0.0

    @property
    def duration_seconds(self) -> float:
        return self.last_seen - self.first_seen


@dataclass(slots=True)
class Observation:
    tracked: TrackedOpportunity
    is_new: bool  # True: a fresh DB row should be created. False: an existing row should be updated.


class OpportunityTracker:
    def __init__(self, liveness_seconds: float = DEFAULT_LIVENESS_SECONDS) -> None:
        self._liveness_seconds = liveness_seconds
        self._active: dict[str, TrackedOpportunity] = {}

    def observe(self, opp: Opportunity, now: float | None = None) -> Observation:
        """Mutates `opp.id` in place to the tracked entry's id on a
        continuation, so every downstream consumer (paper trading, position
        keys) naturally refers to the same underlying opportunity record
        without needing to know whether this was a new sighting or not."""
        now = now if now is not None else time.time()
        key = opportunity_key(opp)
        edge = opp.net_spread_pct if opp.net_spread_pct is not None else opp.gross_spread_pct

        existing = self._active.get(key)
        if existing is not None and (now - existing.last_seen) <= self._liveness_seconds:
            existing.last_seen = now
            existing.current_edge_pct = edge
            existing.max_edge_pct = max(existing.max_edge_pct, edge)
            existing.min_edge_pct = min(existing.min_edge_pct, edge)
            existing._edge_sum += edge
            existing.updates_count += 1
            existing.status = OpportunityStatus.ACTIVE
            opp.id = existing.opportunity_id
            return Observation(existing, is_new=False)

        tracked = TrackedOpportunity(
            key=key,
            opportunity_id=opp.id,
            first_seen=now,
            last_seen=now,
            initial_edge_pct=edge,
            current_edge_pct=edge,
            max_edge_pct=edge,
            min_edge_pct=edge,
            updates_count=1,
            status=OpportunityStatus.DETECTED,
            _edge_sum=edge,
        )
        self._active[key] = tracked
        return Observation(tracked, is_new=True)

    def expire_stale(self, now: float | None = None) -> list[TrackedOpportunity]:
        """Call once per scan. Returns tracked opportunities whose signal has
        gone quiet (spec section 11) so the caller can close them out in the
        DB. Removes them from the active set so a later re-emergence of the
        same key is correctly treated as a brand new opportunity."""
        now = now if now is not None else time.time()
        expired = []
        for key in [k for k, t in self._active.items() if (now - t.last_seen) > self._liveness_seconds]:
            tracked = self._active.pop(key)
            tracked.status = OpportunityStatus.EXPIRED
            expired.append(tracked)
        return expired

    def active_count(self) -> int:
        return len(self._active)
