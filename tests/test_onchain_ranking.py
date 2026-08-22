import uuid

from app.config.constants import Strategy
from app.onchain.ranking import apply_master_ranking_score
from app.opportunity.models import Opportunity


def _opportunity(**overrides) -> Opportunity:
    defaults = dict(
        strategy=Strategy.DEX_CROSS,
        symbol="SOL/USDC",
        legs=[],
        gross_spread_pct=1.0,
        net_spread_pct=0.5,
        capital_usd=1_000.0,
        expected_profit_usd=5.0,
        execution_fill_probability=0.85,
        holding_period_seconds=20.0,
        id=uuid.uuid4(),
    )
    defaults.update(overrides)
    return Opportunity(**defaults)


def test_applies_a_score_on_the_same_scale_cex_opportunities_use():
    opp = _opportunity()
    apply_master_ranking_score(opp)
    assert opp.capital_velocity_score is not None
    assert 0.0 <= opp.capital_velocity_score <= 100.0
    assert opp.return_per_minute_pct is not None


def test_a_faster_higher_probability_opportunity_scores_higher():
    fast = apply_master_ranking_score(_opportunity(holding_period_seconds=10.0, execution_fill_probability=0.95))
    slow = apply_master_ranking_score(_opportunity(holding_period_seconds=600.0, execution_fill_probability=0.5))
    assert fast.capital_velocity_score > slow.capital_velocity_score


def test_missing_required_fields_leaves_score_none_without_crashing():
    opp = _opportunity(capital_usd=None)
    result = apply_master_ranking_score(opp)
    assert result.capital_velocity_score is None


def test_dex_and_cex_shaped_opportunities_are_scored_identically_given_the_same_inputs():
    dex_opp = _opportunity(strategy=Strategy.DEX_TRIANGULAR)
    cex_opp = _opportunity(strategy=Strategy.CROSS_EXCHANGE)
    apply_master_ranking_score(dex_opp)
    apply_master_ranking_score(cex_opp)
    assert dex_opp.capital_velocity_score == cex_opp.capital_velocity_score
