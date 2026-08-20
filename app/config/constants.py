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
    BASIS = "basis"


class MarketType(StrEnum):
    SPOT = "spot"
    PERPETUAL = "perpetual"
    FUTURES = "futures"


class OpportunityClassification(StrEnum):
    NOT_PROFITABLE = "not_profitable"  # net spread <= 0 — a real cost, not just "low priority"
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


class HoldingTimeCategory(StrEnum):
    """Fast-Rotation spec, sections 1 & 12 — every strategy is bucketed by
    how long it ties up capital, independent of which engine produced it."""

    ULTRA_FAST = "ultra_fast"  # < 30s
    FAST = "fast"  # 30s - 5min
    MEDIUM = "medium"  # 5min - 30min
    CARRY = "carry"  # > 30min — the old carry strategies (Funding, Basis)


# Fast-Rotation spec, section 12.
ULTRA_FAST_MAX_SECONDS = 30.0
FAST_MAX_SECONDS = 5 * 60.0
MEDIUM_MAX_SECONDS = 30 * 60.0  # spec section 11's "Maximum Holding Time" — beyond this, it's Carry Mode

# Nominal execution/settlement time assumed for the "instant" round-trip
# strategies (Cross-Exchange, Triangular, Stablecoin) — a real order still
# takes a few seconds to place, fill, and confirm even when nothing is
# "held" in the traditional sense. This also doubles as their minimum
# re-entry cooldown (spec section 40) via the existing position tracker —
# the same opportunity can't be re-opened until it elapses, with no need
# for a second, separate cooldown mechanism.
NOMINAL_FAST_HOLDING_SECONDS = 8.0


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
# Majors (original 5) + altcoins, all verified live and listed as <ASSET>USDT
# / <ASSET>-USDT / <ASSET>USDT on Binance, OKX, and Bybit spot (2026-08-19).
# Smaller-cap names carry wider natural spreads than the majors — more
# chance of a real edge, at the cost of more liquidity/slippage risk (the
# Liquidity/Slippage engines already price that in per opportunity).
CROSS_EXCHANGE_ASSETS = [
    "BTC", "ETH", "SOL", "BNB", "XRP",
    "DOGE", "ADA", "AVAX", "LINK", "DOT", "LTC", "TRX", "NEAR", "ATOM",
    "UNI", "APT", "ARB", "OP", "SUI", "INJ", "SHIB", "PEPE", "ICP", "FIL",
    "AAVE", "RENDER",
]

# Engine D — Spot/Futures Basis Arbitrage. Binance-only for now (dated
# quarterly futures aren't wired up for OKX/Bybit), and limited to the
# assets that actually have a liquid quarterly contract — most alts on
# Binance only list a perpetual, not a dated future.
DELIVERY_FUTURES_ASSETS = ["BTC", "ETH"]

# Section 6 — Engine C: Triangular Arbitrage
# Extra symbols (beyond X/USDT) that must be streamed for triangular loops to close.
# FDUSD/USDC and FDUSD/USDT verified live on Binance (2026-08-20) — FDUSD is
# Binance-only (confirmed absent from OKX/Bybit again on the same date), so
# it can't feed the cross-exchange Stablecoin engine, but a same-exchange
# triangular loop only needs it listed on the one exchange it runs on.
TRIANGULAR_CROSS_PAIRS = ["ETH/BTC", "SOL/BTC", "BNB/BTC", "XRP/BTC", "FDUSD/USDC", "FDUSD/USDT"]
# Each path is (base, leg1_asset, leg2_asset): base -> leg1 -> leg2 -> base.
TRIANGULAR_PATHS: list[tuple[str, str, str]] = [
    ("USDT", "BTC", "ETH"),
    ("USDT", "BTC", "SOL"),
    ("USDT", "BTC", "BNB"),
    ("USDT", "BTC", "XRP"),
    ("USDT", "USDC", "FDUSD"),  # pure stablecoin triangle: USDT -> USDC -> FDUSD -> USDT
]

# Representative capital used to price a freshly detected opportunity before
# it's replayed against each Virtual Portfolio (section 20) in paper trading.
DEFAULT_OPPORTUNITY_CAPITAL_USD = 1_000.0

# Break-Even Spread Engine — costs beyond trading fees that aren't measured
# per-opportunity yet (real slippage/rebalancing are priced in separately by
# the Liquidity/Rebalancing engines where wired up). These are conservative,
# configurable placeholder assumptions, not measurements.
DEFAULT_SLIPPAGE_BUFFER_PCT = 0.02
DEFAULT_REBALANCING_BUFFER_PCT = 0.01
DEFAULT_SAFETY_MARGIN_PCT = 0.03

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
