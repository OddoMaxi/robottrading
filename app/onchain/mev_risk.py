"""MEV Risk Score (Multi-Market Opportunity Engine, V5.5, spec section 13).

Explicitly NOT an attempt to predict MEV perfectly — the spec's own words
("Do NOT pretend to predict MEV perfectly. Use conservative buffers.").
This is a conservative, documented heuristic used to WIDEN the flat MEV
buffer (app.onchain.constants.MEV_BUFFER_PCT) for genuinely higher-risk
trade shapes, not a forecast of whether any specific transaction will
actually get front-run or sandwiched.
"""

from app.onchain.constants import MEV_BUFFER_PCT

# A trade at this fraction (or more) of a pool's TVL is treated as
# maximally MEV-attractive by size alone — big enough to move the price
# meaningfully, which is exactly what makes sandwiching profitable for an
# attacker. Documented threshold, not a measurement.
SIZE_FRACTION_FOR_MAX_RISK = 0.05

# Ethereum's MEV/searcher/builder ecosystem is the most developed and
# competitive of the three chains here (well documented publicly); BSC and
# Solana less so, though both have real, active searcher activity. A
# documented per-chain assumption, not a live measurement.
_CHAIN_MEV_COMPETITIVENESS_SCORE = {"eth": 0.9, "bsc": 0.6, "solana": 0.5}

# The MEV buffer scales between these two bounds by the risk score — a
# risk_score of 0 still charges the floor (spec's own MEV_BUFFER_PCT is
# never waived to zero; front-running risk is never truly zero), a
# risk_score of 1 charges 3x that floor.
_MEV_BUFFER_MAX_MULTIPLIER = 3.0


def compute_mev_risk_score(chain: str, trade_size_usd: float, pool_tvl_usd: float) -> float:
    """0-1, higher = more MEV risk. Combines how large this trade is
    relative to the pool it trades against (bigger relative size both
    moves the price more and is more profitable to sandwich) with how
    MEV-competitive the chain's own searcher ecosystem is."""
    size_fraction = (trade_size_usd / pool_tvl_usd) if pool_tvl_usd > 0 else 1.0
    size_risk = min(1.0, size_fraction / SIZE_FRACTION_FOR_MAX_RISK)
    chain_risk = _CHAIN_MEV_COMPETITIVENESS_SCORE.get(chain, 0.7)
    return round(min(1.0, 0.6 * size_risk + 0.4 * chain_risk), 3)


def mev_buffer_pct_for_risk(risk_score: float) -> float:
    """Scales the flat MEV buffer by the risk score — never below the
    documented floor, up to 3x it for the highest-risk shapes."""
    risk_score = max(0.0, min(1.0, risk_score))
    return MEV_BUFFER_PCT * (1.0 + risk_score * (_MEV_BUFFER_MAX_MULTIPLIER - 1.0))
