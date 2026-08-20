import pytest

from app.config.constants import Strategy
from app.engines.base import ArbitrageEngine
from app.opportunity.detector import OpportunityDetector
from app.opportunity.models import Opportunity


class _FakeEngine(ArbitrageEngine):
    strategy_name = Strategy.CROSS_EXCHANGE

    def __init__(self, opportunities: list[Opportunity]) -> None:
        self._opportunities = opportunities

    async def detect(self) -> list[Opportunity]:
        return self._opportunities


def make_opp(symbol: str, holding_period_seconds: float, capital_usd: float, net_spread_pct: float = 0.3) -> Opportunity:
    expected_profit_usd = capital_usd * net_spread_pct / 100
    return Opportunity(
        strategy=Strategy.CROSS_EXCHANGE,
        symbol=symbol,
        legs=[],
        gross_spread_pct=net_spread_pct + 0.1,
        net_spread_pct=net_spread_pct,
        capital_usd=capital_usd,
        expected_profit_usd=expected_profit_usd,
        holding_period_seconds=holding_period_seconds,
        execution_fill_probability=1.0,
    )


@pytest.mark.asyncio
async def test_scan_once_ranks_by_capital_velocity_not_raw_profit():
    """Spec section 15 — a small trade that recycles capital in seconds
    should outrank a bigger trade with the same raw profit but a much
    longer hold, since the fast one can be repeated far more often."""
    slow_big = make_opp("SLOW/USDT", holding_period_seconds=3600.0, capital_usd=1_000.0)  # $3 profit, held 1h
    fast_small = make_opp("FAST/USDT", holding_period_seconds=8.0, capital_usd=1_000.0)  # $3 profit, held 8s

    detector = OpportunityDetector([_FakeEngine([slow_big, fast_small])])
    opportunities = await detector.scan_once()

    assert [o.symbol for o in opportunities] == ["FAST/USDT", "SLOW/USDT"]
    assert opportunities[0].capital_velocity_score > opportunities[1].capital_velocity_score
