"""Chain Risk / Chain Health (Multi-Market Opportunity Engine, V5.5, spec section 30).

"If chain is degraded: increase risk buffer or stop new opportunities."
This module classifies each chain into one of 4 states from real, live
signals — an unreachable RPC (UNAVAILABLE) or an elevated real gas price
relative to that chain's own documented normal range (CONGESTED /
DEGRADED, the two escalating severities) — never a fabricated status.

Gas price (not its USD conversion) is what the classification actually
reads: a token price spike unrelated to network congestion would
otherwise get misread as "congestion" if the threshold were in USD terms.
"""

import logging
from enum import StrEnum

import aiohttp

from app.onchain.gas_engine import _fetch_evm_gas_price_wei

logger = logging.getLogger(__name__)

SOLANA_HEALTH_RPC_URL = "https://api.mainnet-beta.solana.com"
_RPC_TIMEOUT_SECONDS = 8.0

# (congested_above_gwei, degraded_above_gwei) — documented "normal" ranges
# per chain; a real value above the first threshold means gas is
# meaningfully elevated (still usable, worse economics), above the second
# means the network is under enough load that shadow detection should
# stand down for that chain this cycle rather than price against a gas
# assumption that's already stale by the time anything could execute.
_GAS_PRICE_THRESHOLDS_GWEI = {"eth": (40.0, 100.0), "bsc": (7.0, 15.0)}


class ChainHealth(StrEnum):
    HEALTHY = "healthy"
    CONGESTED = "congested"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


def classify_evm_chain_health(chain: str, gas_price_gwei: float | None) -> ChainHealth:
    """Pure classification step — unit-testable without a network call,
    same pattern used throughout this codebase (e.g.
    app.reporting.simple_summary.classify_robot_health)."""
    if gas_price_gwei is None:
        return ChainHealth.UNAVAILABLE
    congested_above, degraded_above = _GAS_PRICE_THRESHOLDS_GWEI.get(chain, (40.0, 100.0))
    if gas_price_gwei > degraded_above:
        return ChainHealth.DEGRADED
    if gas_price_gwei > congested_above:
        return ChainHealth.CONGESTED
    return ChainHealth.HEALTHY


async def _check_solana_reachable() -> bool:
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                SOLANA_HEALTH_RPC_URL,
                json={"jsonrpc": "2.0", "id": 1, "method": "getHealth"},
                timeout=aiohttp.ClientTimeout(total=_RPC_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        return payload.get("result") == "ok"
    except Exception as exc:
        logger.warning("chain risk: solana health check failed: %s", exc)
        return False


async def check_chain_health(chain: str) -> ChainHealth:
    """Solana's congestion doesn't manifest as a simple gas-price signal
    the way EVM chains' does (it would need priority-fee percentile data
    this module doesn't fetch — real future work, not fabricated here) —
    reachability is the one honest signal available today, so a reachable
    Solana RPC reports HEALTHY, an unreachable one reports UNAVAILABLE."""
    if chain == "solana":
        reachable = await _check_solana_reachable()
        return ChainHealth.HEALTHY if reachable else ChainHealth.UNAVAILABLE

    gas_price_wei = await _fetch_evm_gas_price_wei(chain)
    gas_price_gwei = (gas_price_wei / 1e9) if gas_price_wei is not None else None
    return classify_evm_chain_health(chain, gas_price_gwei)
