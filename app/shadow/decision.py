"""Shadow Master Decision — Global Capital Allocator SIMULATION (Phase 2,
SHADOW MODE ONLY).

For one opportunity: would the Master Ranker + Global Capital Allocator
have allocated capital to it, against the SAME shared shadow pool every
other opportunity (CEX or DEX) competes against? This is the entire
point of Phase 2 shadow mode — proving cross-engine capital contention
CAN be arbitrated centrally, without yet letting it actually happen.

master's projected_net_profit_usd is the opportunity's own already-
computed expected value (app.shadow.ranker.compute_expected_value_usd) —
credited to the shadow ledger the moment capital is allocated. This is a
PROJECTION, not a resampled execution outcome the way
app.simulation.paper_trader/app.onchain.dex_paper_trader simulate a real
fill/fail roll — shadow mode has no live opportunity to revalidate
against (it evaluates already-historical, already-detected rows), so
crediting the real engines' own already-computed EV is the most honest
number available, never a fabricated random result.
"""

import uuid
from datetime import datetime

from app.shadow.ledger import ShadowCapitalLedger
from app.shadow.models import MasterDecision, MasterOutcome, ShadowOpportunitySummary
from app.shadow.positions import ShadowOpenPositionTracker, position_key_for
from app.shadow.ranker import compute_expected_value_usd, compute_master_rank_score

DEFAULT_HOLDING_SECONDS = 60.0  # fallback capital-lock duration when an opportunity carries none


def duplicate_economic_event_decision(rank_score: float, winner_opportunity_id: uuid.UUID, capital_available_global_usd: float) -> MasterDecision:
    """PRE-PHASE-2 CORRECTIVE MAINTENANCE #2 (fix 1): built by the caller
    (app.shadow.dedup has already identified this opportunity as the
    lower-ranked twin of `winner_opportunity_id`) — never independently
    evaluated for capital, exactly mirroring how main.py's real dedup fix
    excludes the loser from ever reaching attempt_dex_trade."""
    return MasterDecision(
        outcome=MasterOutcome.REJECT_DUPLICATE_ECONOMIC_EVENT,
        reason=f"same economic event (legs+detected_at) as opportunity {winner_opportunity_id}, lower rank score",
        rank_score=rank_score,
        theoretical_capital_reserved_usd=None,
        projected_net_profit_usd=None,
        capital_available_global_usd=capital_available_global_usd,
    )


def evaluate_shadow_decision(
    opp: ShadowOpportunitySummary, ledger: ShadowCapitalLedger, position_tracker: ShadowOpenPositionTracker, now: datetime
) -> MasterDecision:
    now_epoch = now.timestamp()
    available_now = ledger.available_capital_usd(now_epoch)
    rank_score = compute_master_rank_score(opp)
    ev = compute_expected_value_usd(opp)

    # PRE-PHASE-2 CORRECTIVE MAINTENANCE #2 (fix 2): reproduces
    # app.execution.validator.validate()'s own position_already_open gate
    # — checked BEFORE capital, matching the real gate's own early exit,
    # since an already-open position is rejected regardless of how much
    # capital happens to be free.
    if opp.holding_period_seconds is not None:
        position_key = position_key_for(opp.strategy, opp.legs, opp.symbol)
        if position_key is not None and position_tracker.is_open(position_key, now_epoch):
            return MasterDecision(
                outcome=MasterOutcome.REJECT_POSITION_ALREADY_OPEN,
                reason=f"position already open for key {position_key}",
                rank_score=rank_score if rank_score is not None else 0.0,
                theoretical_capital_reserved_usd=None,
                projected_net_profit_usd=None,
                capital_available_global_usd=available_now,
            )

    if ev is None or opp.capital_usd is None or rank_score is None:
        return MasterDecision(
            outcome=MasterOutcome.REJECT_MISSING_DATA,
            reason="missing expected_profit_usd, capital_usd, or a rankable score",
            rank_score=rank_score if rank_score is not None else 0.0,
            theoretical_capital_reserved_usd=None,
            projected_net_profit_usd=None,
            capital_available_global_usd=available_now,
        )

    if ev <= 0:
        return MasterDecision(
            outcome=MasterOutcome.REJECT_NOT_PROFITABLE,
            reason=f"expected value {ev:.4f} <= 0",
            rank_score=rank_score,
            theoretical_capital_reserved_usd=None,
            projected_net_profit_usd=None,
            capital_available_global_usd=available_now,
        )

    # Sizes DOWN to whatever's actually available, matching the already-
    # established behavior of both real engines
    # (attempt_dex_trade/paper_trader.simulate) — a smaller real
    # allocation beats rejecting outright when only PART of the requested
    # capital is free.
    capital_requested = min(opp.capital_usd, available_now)
    if capital_requested <= 0:
        return MasterDecision(
            outcome=MasterOutcome.REJECT_NO_CAPITAL,
            reason=f"requested {opp.capital_usd:.2f}, {max(available_now, 0.0):.2f} available in the shared shadow pool",
            rank_score=rank_score,
            theoretical_capital_reserved_usd=None,
            projected_net_profit_usd=None,
            capital_available_global_usd=available_now,
        )

    release_at = now_epoch + (opp.holding_period_seconds or DEFAULT_HOLDING_SECONDS)
    reserved = ledger.reserve(opp.opportunity_id, capital_requested, now_epoch, release_at)
    if not reserved:
        # Guards against a race between the availability check above and
        # the reservation call — should not happen in this single-threaded
        # evaluator, kept as the same defense-in-depth pattern
        # DexCapitalPool/VirtualPortfolio.lock_capital both use.
        return MasterDecision(
            outcome=MasterOutcome.REJECT_NO_CAPITAL,
            reason="reservation failed at the atomic check",
            rank_score=rank_score,
            theoretical_capital_reserved_usd=None,
            projected_net_profit_usd=None,
            capital_available_global_usd=available_now,
        )

    ev_scaled = ev * (capital_requested / opp.capital_usd)
    ledger.resolve_pnl(ev_scaled)

    if opp.holding_period_seconds is not None:
        position_key = position_key_for(opp.strategy, opp.legs, opp.symbol)
        if position_key is not None:
            position_tracker.open_position(position_key, now_epoch, opp.holding_period_seconds)

    return MasterDecision(
        outcome=MasterOutcome.ALLOCATE,
        reason=None,
        rank_score=rank_score,
        theoretical_capital_reserved_usd=capital_requested,
        projected_net_profit_usd=ev_scaled,
        capital_available_global_usd=available_now,
    )


def decisions_agree(old_approved: bool, master_approved: bool) -> bool:
    return old_approved == master_approved
