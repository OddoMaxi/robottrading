"""Static reference values from the cahier des charges (V1).

These are starting points, not tuned parameters — sections 16 and 26 of the
spec call for recalibration after the 7-day observation window.
"""

from enum import StrEnum


class Strategy(StrEnum):
    STABLECOIN = "stablecoin"
    CROSS_EXCHANGE = "cross_exchange"
    TRIANGULAR = "triangular"
    FUNDING = "funding"


class MarketType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURES = "futures"


class OpportunityClassification(StrEnum):
    WATCH = "watch"
    INTERESTING = "interesting"
    GOOD = "good"
    STRONG = "strong"
    EXCEPTIONAL = "exceptional"


class OpportunityStatus(StrEnum):
    DETECTED = "detected"
    OPEN = "open"
    CLOSED = "closed"
    EXPIRED = "expired"


# Section 3 — Platforms
PRIORITY_EXCHANGES = ["binance", "okx", "bybit"]
NEXT_PHASE_EXCHANGES = ["kraken", "gateio", "kucoin"]

# Section 4 — Engine A: Stablecoin Arbitrage
# Verified live against each exchange's REST API (2026-08-19): USDC/USDT is
# the only one of the three spec pairs actually listed on all 3 priority
# exchanges. FDUSD is Binance-only (FDUSDUSDT, FDUSDUSDC) — not comparable
# cross-exchange until another priority exchange lists it.
STABLECOIN_PAIRS = ["USDC/USDT"]

# Section 5 — Engine B: Cross-Exchange Arbitrage
CROSS_EXCHANGE_ASSETS = ["BTC", "ETH", "SOL", "BNB", "XRP"]

# Section 6 — Engine C: Triangular Arbitrage
# Extra symbols (beyond X/USDT) that must be streamed for triangular loops to close.
TRIANGULAR_CROSS_PAIRS = ["ETH/BTC", "SOL/BTC", "BNB/BTC", "XRP/BTC"]
# Each path is (base, leg1_asset, leg2_asset): base -> leg1 -> leg2 -> base.
TRIANGULAR_PATHS: list[tuple[str, str, str]] = [
    ("USDT", "BTC", "ETH"),
    ("USDT", "BTC", "SOL"),
    ("USDT", "BTC", "BNB"),
    ("USDT", "BTC", "XRP"),
]

# Representative capital used to price a freshly detected opportunity before
# it's replayed against each Virtual Portfolio (section 20) in paper trading.
DEFAULT_OPPORTUNITY_CAPITAL_USD = 1_000.0

# Section 12 — Liquidity Engine test amounts (USD)
LIQUIDITY_TEST_AMOUNTS_USD = [100, 250, 500, 1_000, 2_500, 5_000, 10_000, 25_000]

# Section 20 — Virtual Portfolios (USD)
VIRTUAL_PORTFOLIO_SIZES_USD = {
    "500": 500,
    "1K": 1_000,
    "5K": 5_000,
    "10K": 10_000,
    "25K": 25_000,
}

# Section 16 — Opportunity Classification thresholds (net spread %, lower bound inclusive)
CLASSIFICATION_THRESHOLDS: dict[OpportunityClassification, float] = {
    OpportunityClassification.WATCH: 0.00,
    OpportunityClassification.INTERESTING: 0.05,
    OpportunityClassification.GOOD: 0.10,
    OpportunityClassification.STRONG: 0.20,
    OpportunityClassification.EXCEPTIONAL: 0.40,
}
