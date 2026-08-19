"""In-memory representation of a detected opportunity, before persistence.

Shared by all four engines (app/engines/) and by the Opportunity Engine
(detector.py) that aggregates them. The database/models.py Opportunity table
mirrors this shape for persistence.
"""

import uuid
from dataclasses import dataclass, field

from app.config.constants import OpportunityClassification, OpportunityStatus, Strategy


@dataclass(slots=True)
class Opportunity:
    strategy: Strategy
    symbol: str  # e.g. "BTC/USDT" or "USDT/USDC"
    legs: list[dict]  # ordered list of {exchange, side, market, price, quantity}

    gross_spread_pct: float
    net_spread_pct: float | None = None  # filled in once Fee/Liquidity/Slippage engines run
    break_even_pct: float | None = None  # minimum gross spread that would have cleared all costs

    capital_usd: float | None = None
    expected_profit_usd: float | None = None

    # Maker/Taker Strategy Engine (informational — net_spread_pct/expected_profit_usd
    # above stay the certain-fill taker/taker baseline; these describe the
    # best of the 4 execution modes by probability-weighted expected value).
    execution_mode: str | None = None
    execution_fill_probability: float | None = None

    # False Opportunity Filter (section 18) — how old the underlying quotes
    # were when this opportunity was priced.
    market_data_age_seconds: float | None = None

    # Basis Engine — meaningful for a dated future (basis must converge to
    # zero by expiry, so annualizing it is a real yield figure, unlike a
    # perpetual's basis).
    annualized_pct: float | None = None
    days_to_expiry: float | None = None

    score: float | None = None  # 0-100, section 15
    classification: OpportunityClassification | None = None

    detected_at: float = 0.0
    peak_at: float | None = None
    closed_at: float | None = None
    max_spread_pct: float | None = None
    avg_spread_pct: float | None = None

    status: OpportunityStatus = OpportunityStatus.DETECTED
    id: uuid.UUID = field(default_factory=uuid.uuid4)
