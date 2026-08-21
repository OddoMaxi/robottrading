from app.config.constants import OpportunityClassification, Strategy
from app.execution.validator import RejectionReason, validate
from app.opportunity.models import Opportunity
from app.simulation.position_tracker import OpenPositionTracker


def make_opp(**overrides) -> Opportunity:
    defaults = dict(
        strategy=Strategy.CROSS_EXCHANGE,
        symbol="BTC/USDT",
        legs=[{"exchange": "binance", "side": "buy", "market": "spot"}],
        gross_spread_pct=0.5,
        net_spread_pct=0.3,
        classification=OpportunityClassification.GOOD,
        market_data_age_seconds=0.1,
        holding_period_seconds=8.0,
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def test_approves_a_healthy_good_opportunity():
    result = validate(make_opp(), OpenPositionTracker(), now=1_000.0)
    assert result.approved is True
    assert result.reason is None


def test_rejects_stale_data_before_anything_else():
    opp = make_opp(market_data_age_seconds=10.0, classification=OpportunityClassification.NOT_PROFITABLE)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.approved is False
    assert result.reason == RejectionReason.STALE_DATA


def test_rejects_not_profitable_as_fees_too_high():
    opp = make_opp(classification=OpportunityClassification.NOT_PROFITABLE, net_spread_pct=-0.05)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.reason == RejectionReason.FEES_TOO_HIGH


def test_rejects_watch_classification_as_edge_too_low():
    opp = make_opp(classification=OpportunityClassification.WATCH, net_spread_pct=0.01)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.reason == RejectionReason.EDGE_TOO_LOW


def test_rejects_missing_classification_as_edge_too_low():
    opp = make_opp(classification=None)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.reason == RejectionReason.EDGE_TOO_LOW


def test_rejects_when_a_position_is_already_open_on_the_same_key():
    tracker = OpenPositionTracker()
    tracker.open_position(("cross_exchange", "binance", "BTC/USDT"), now=1_000.0, holding_period_seconds=60.0)

    opp = make_opp()
    result = validate(opp, tracker, now=1_010.0)
    assert result.reason == RejectionReason.POSITION_ALREADY_OPEN


def test_approves_once_the_open_position_has_expired():
    tracker = OpenPositionTracker()
    tracker.open_position(("cross_exchange", "binance", "BTC/USDT"), now=1_000.0, holding_period_seconds=60.0)

    opp = make_opp()
    result = validate(opp, tracker, now=1_070.0)  # 70s later, past the 60s hold
    assert result.approved is True


def test_opportunity_with_no_holding_period_skips_the_position_check():
    opp = make_opp(holding_period_seconds=None)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.approved is True


def test_rejects_a_holding_period_over_20_minutes_as_too_long():
    """FAST TRADING ONLY (user directive, 2026-08-21) — a brand new
    position is never opened with an expected hold beyond 20 minutes,
    which is what excludes Basis/Funding-style multi-hour-to-multi-week
    strategies from ever reaching validate() in the first place, even if
    one were re-enabled by mistake."""
    opp = make_opp(holding_period_seconds=1_201.0)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.approved is False
    assert result.reason == RejectionReason.HOLDING_TOO_LONG


def test_approves_a_holding_period_at_exactly_20_minutes():
    opp = make_opp(holding_period_seconds=1_200.0)
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.approved is True


def test_a_multi_week_basis_style_holding_period_is_rejected():
    opp = make_opp(holding_period_seconds=3_123_377.0)  # ~36 days, matching the legacy position found live
    result = validate(opp, OpenPositionTracker(), now=1_000.0)
    assert result.approved is False
    assert result.reason == RejectionReason.HOLDING_TOO_LONG
