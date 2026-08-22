import uuid
from datetime import UTC, datetime

from app.shadow.models import Engine, ShadowOpportunitySummary
from app.shadow.ranker import compute_expected_value_usd, compute_master_rank_score


def _opp(**overrides) -> ShadowOpportunitySummary:
    defaults = dict(
        opportunity_id=uuid.uuid4(),
        engine=Engine.CEX,
        strategy="cross_exchange",
        symbol="BTC/USDT",
        legs=[{"exchange": "binance"}, {"exchange": "bybit"}],
        chain=None,
        expected_profit_usd=5.0,
        capital_usd=1_000.0,
        execution_fill_probability=0.9,
        capital_velocity_score=42.0,
        holding_period_seconds=30.0,
        detected_at=datetime.now(UTC).replace(tzinfo=None),
        detection_time_rejection_reason=None,
    )
    defaults.update(overrides)
    return ShadowOpportunitySummary(**defaults)


def test_compute_expected_value_weights_by_fill_probability():
    opp = _opp(expected_profit_usd=10.0, execution_fill_probability=0.5)
    assert compute_expected_value_usd(opp) == 5.0


def test_compute_expected_value_defaults_fill_probability_to_one():
    opp = _opp(expected_profit_usd=3.0, execution_fill_probability=None)
    assert compute_expected_value_usd(opp) == 3.0


def test_compute_expected_value_none_when_no_expected_profit():
    opp = _opp(expected_profit_usd=None)
    assert compute_expected_value_usd(opp) is None


def test_master_rank_score_prefers_existing_capital_velocity_score():
    opp = _opp(capital_velocity_score=77.0)
    assert compute_master_rank_score(opp) == 77.0


def test_master_rank_score_falls_back_when_velocity_score_missing():
    opp = _opp(capital_velocity_score=None, expected_profit_usd=10.0, capital_usd=1000.0, holding_period_seconds=60.0)
    score = compute_master_rank_score(opp)
    assert score is not None
    assert score > 0


def test_master_rank_score_none_when_nothing_to_rank_on():
    opp = _opp(capital_velocity_score=None, expected_profit_usd=None, capital_usd=None)
    assert compute_master_rank_score(opp) is None


def test_master_rank_score_higher_ev_per_capital_minute_ranks_higher():
    fast_small = _opp(capital_velocity_score=None, expected_profit_usd=5.0, capital_usd=500.0, holding_period_seconds=30.0)
    slow_big = _opp(capital_velocity_score=None, expected_profit_usd=5.0, capital_usd=5000.0, holding_period_seconds=600.0)
    assert compute_master_rank_score(fast_small) > compute_master_rank_score(slow_big)
