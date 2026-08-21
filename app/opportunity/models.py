"""In-memory representation of a detected opportunity, before persistence.

Shared by all four engines (app/engines/) and by the Opportunity Engine
(detector.py) that aggregates them. The database/models.py Opportunity table
mirrors this shape for persistence.
"""

import uuid
from dataclasses import dataclass, field

from app.config.constants import HoldingTimeCategory, OpportunityClassification, OpportunityStatus, Strategy
from app.opportunity.holding_time import classify_holding_time


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

    # Depth-Adjusted Execution Curve (Opportunity Expansion spec, Step 2,
    # user directive, 2026-08-21) — for a liquidity-capped opportunity
    # (Cross-Exchange/Stablecoin today), capital_usd/net_spread_pct/
    # expected_profit_usd above are now priced AT optimal_capital_usd (the
    # size that maximizes absolute net profit against real order-book
    # depth on both legs), not a fixed default — so realistic_executable_edge_pct
    # always equals net_spread_pct there; kept as its own explicitly-named
    # field so a caller doesn't need to know that wiring detail to read it.
    # theoretical_edge_pct is the raw top-of-book spread (mirrors
    # gross_spread_pct); depth_adjusted_edge_pct is what net_spread_pct
    # would have been at the naive fixed intended size, before optimizing —
    # the three together are what let a glance tell "the headline number"
    # from "what we'd actually net if we sized this well".
    theoretical_edge_pct: float | None = None
    depth_adjusted_edge_pct: float | None = None
    realistic_executable_edge_pct: float | None = None
    optimal_capital_usd: float | None = None
    max_profitable_capital_usd: float | None = None

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

    # How long this position ties up capital once opened. Every engine sets
    # this now (Fast-Rotation spec) — Cross-Exchange/Triangular/Stablecoin
    # use a short nominal execution-time estimate (NOMINAL_FAST_HOLDING_SECONDS),
    # Funding/Basis use their real multi-day carry period. None only for an
    # opportunity built outside the normal engines (e.g. in a test).
    holding_period_seconds: float | None = None
    # Derived from holding_period_seconds in __post_init__ — Fast vs Carry
    # Mode split (spec section 1) reads this, not the strategy name, since
    # the same strategy could in principle land in either bucket.
    holding_time_category: HoldingTimeCategory | None = None
    # True (default) when capital_usd was derived from a real VWAP fill
    # against observed order-book depth (Cross-Exchange, Triangular,
    # Stablecoin) — the Paper Trader must never scale capital *above* that,
    # since it would pretend the book can absorb more than what was actually
    # observed. False for Basis/Funding, where capital_usd is just the fixed
    # detection-time size (no depth data for the futures/perp leg exists to
    # cap it against) — those *can* scale up to a portfolio's available capital.
    capital_is_liquidity_capped: bool = True

    score: float | None = None  # 0-100, section 15
    classification: OpportunityClassification | None = None

    # Fast-Rotation spec, sections 13-15 — ranks capital efficiency, not just
    # raw profit; a small fast trade can beat a bigger slower one.
    capital_velocity_score: float | None = None  # 0-100
    return_per_minute_pct: float | None = None

    detected_at: float = 0.0
    peak_at: float | None = None
    closed_at: float | None = None
    max_spread_pct: float | None = None
    avg_spread_pct: float | None = None

    status: OpportunityStatus = OpportunityStatus.DETECTED
    # Continuous Execution spec, sections 12-15, 43 — set by
    # app.execution.validator.validate() once the detection loop runs it
    # through the gate. None while still unvalidated, or once APPROVED.
    rejection_reason: str | None = None
    id: uuid.UUID = field(default_factory=uuid.uuid4)

    def __post_init__(self) -> None:
        if self.holding_period_seconds is not None and self.holding_time_category is None:
            self.holding_time_category = classify_holding_time(self.holding_period_seconds)
