"""Opportunity Engine (section 10) — aggregates all four engines into one stream."""

import time

from app.analytics.capital_velocity import capital_velocity_score, return_per_minute
from app.config.constants import DEFAULT_OPPORTUNITY_CAPITAL_USD, Strategy
from app.engines.base import ArbitrageEngine
from app.opportunity.models import Opportunity
from app.opportunity.scorer import ScoreFactors, score as compute_score
from app.opportunity.validator import classify


class OpportunityDetector:
    def __init__(self, engines: list[ArbitrageEngine]) -> None:
        self.engines = engines

    async def scan_once(self) -> list[Opportunity]:
        """Run detect() on every engine and return the combined, classified list.

        Net spread / fees / liquidity adjustment happens inside each engine's
        detect() before this point — this method stamps detection time and
        applies the shared classification and scoring formulas.
        """
        opportunities: list[Opportunity] = []
        for engine in self.engines:
            for opp in await engine.detect():
                opp.detected_at = time.time()
                if opp.net_spread_pct is not None:
                    opp.classification = classify(opp.net_spread_pct)
                    opp.score = self._score(opp)
                    self._score_velocity(opp)
                opportunities.append(opp)
        return opportunities

    @staticmethod
    def _score_velocity(opp: Opportunity) -> None:
        """Fast-Rotation spec, sections 13-15 — capital efficiency, not just raw profit."""
        if opp.holding_period_seconds is None or opp.capital_usd is None or opp.expected_profit_usd is None:
            return
        opp.return_per_minute_pct = return_per_minute(opp.net_spread_pct, opp.holding_period_seconds)
        opp.capital_velocity_score, _ = capital_velocity_score(
            net_profit_usd=opp.expected_profit_usd,
            execution_probability=opp.execution_fill_probability if opp.execution_fill_probability is not None else 1.0,
            holding_time_seconds=opp.holding_period_seconds,
            capital_usd=opp.capital_usd,
        )

    @staticmethod
    def _score(opp: Opportunity) -> float:
        # Duration/volatility/latency history isn't tracked yet (needs the
        # Duration Engine and a few days of data) — held at a neutral 0.5
        # until then, per section 15's note that V1 uses a deterministic formula.
        fill_ratio = max(0.0, min((opp.capital_usd or 0) / DEFAULT_OPPORTUNITY_CAPITAL_USD, 1.0))
        factors = ScoreFactors(
            net_profit=max(0.0, min((opp.net_spread_pct or 0) / 0.5, 1.0)),  # 0.5% net treated as "excellent"
            liquidity=fill_ratio,
            duration=0.5,
            volatility=0.5,
            slippage=0.8,
            depth=fill_ratio,
            latency=0.5,
            exchange_risk=0.7,
            execution_complexity=0.6 if opp.strategy == Strategy.TRIANGULAR else 0.9,
        )
        return compute_score(factors)
