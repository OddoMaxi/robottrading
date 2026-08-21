"""Dynamic Gas Engine (Multi-Market Opportunity Engine, V5.5, spec section 12).

Real gas price, fetched live via each chain's own public RPC (no API key)
— never a fixed constant, per the spec's own instruction. Converts to USD
using the chain's native token price, which the caller already has from
discovered DEX pools (e.g. a WETH/USDC pool's own price) rather than this
module doing a second, redundant lookup.

Gas UNITS per transaction type are documented, standard estimates for a
simple AMM swap — not a live per-transaction simulation (that needs an
actual `eth_estimateGas` call against a specific calldata payload, real
future work once real execution is ever built) — flagged as such rather
than presented as more precise than it is.
"""

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass
from enum import StrEnum

import aiohttp

logger = logging.getLogger(__name__)

# Standard, documented gas-unit estimates for a simple swap on each chain —
# not a live eth_estimateGas simulation (spec section 12 calls that out as
# real future work). A multi-hop route or an atomic multi-leg transaction
# costs more than a single swap; TRANSACTION_COMPLEXITY_MULTIPLIER below
# scales this estimate rather than requiring a second constant per shape.
_SWAP_GAS_UNITS = {"eth": 180_000, "bsc": 180_000}
TRANSACTION_COMPLEXITY_MULTIPLIER = {"simple_swap": 1.0, "multi_hop": 1.8, "atomic_multi_leg": 2.5}

# Solana's fee model is fundamentally different (spec section 15) — a fixed
# per-signature base fee, no per-instruction "gas units" concept the way
# EVM chains have. 5000 lamports/signature is Solana's own protocol
# constant; a typical swap transaction carries 1-2 signatures. A priority
# fee can be added on top but isn't included here (highly variable,
# congestion-dependent — would need Solana's own getRecentPrioritizationFees
# RPC method, real future work, not fabricated here).
SOLANA_BASE_FEE_LAMPORTS_PER_SIGNATURE = 5_000
SOLANA_SIGNATURES_PER_SWAP = 2
LAMPORTS_PER_SOL = 1_000_000_000

_EVM_RPC_URLS = {
    "eth": "https://ethereum-rpc.publicnode.com",
    "bsc": "https://bsc-dataseed.binance.org",
}
_RPC_TIMEOUT_SECONDS = 8.0


class Chain(StrEnum):
    ETH = "eth"
    BSC = "bsc"
    SOLANA = "solana"


@dataclass(slots=True)
class GasEstimate:
    chain: str
    gas_cost_native: float  # in the chain's native unit (ETH, BNB, SOL)
    gas_cost_usd: float
    complexity: str


class DEXGasProvider(ABC):
    @abstractmethod
    async def estimate_gas_cost_usd(self, chain: str, native_token_price_usd: float, complexity: str = "simple_swap") -> GasEstimate: ...


async def _fetch_evm_gas_price_wei(chain: str) -> float | None:
    url = _EVM_RPC_URLS.get(chain)
    if url is None:
        return None
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url,
                json={"jsonrpc": "2.0", "method": "eth_gasPrice", "params": [], "id": 1},
                timeout=aiohttp.ClientTimeout(total=_RPC_TIMEOUT_SECONDS),
            ) as resp:
                resp.raise_for_status()
                payload = await resp.json()
        result = payload.get("result")
        return float(int(result, 16)) if result else None
    except Exception as exc:
        logger.warning("gas engine: %s RPC gas price fetch failed: %s", chain, exc)
        return None


class RpcGasProvider(DEXGasProvider):
    """Real RPC-backed gas estimates for eth/bsc; Solana's fixed
    per-signature fee needs no RPC round-trip at all."""

    async def estimate_gas_cost_usd(self, chain: str, native_token_price_usd: float, complexity: str = "simple_swap") -> GasEstimate:
        multiplier = TRANSACTION_COMPLEXITY_MULTIPLIER.get(complexity, 1.0)

        if chain == Chain.SOLANA:
            lamports = SOLANA_BASE_FEE_LAMPORTS_PER_SIGNATURE * SOLANA_SIGNATURES_PER_SWAP * multiplier
            gas_cost_native = lamports / LAMPORTS_PER_SOL
            return GasEstimate(chain, gas_cost_native, gas_cost_native * native_token_price_usd, complexity)

        gas_price_wei = await _fetch_evm_gas_price_wei(chain)
        if gas_price_wei is None:
            # No fabricated fallback number — a caller that can't get a
            # real gas price has no basis to claim a DEX opportunity is
            # profitable net of it, so it must not silently substitute a
            # guess. Zero cost would be worse (understates risk); instead
            # this is surfaced as a failure the caller must handle.
            raise RuntimeError(f"gas engine: no live gas price available for chain={chain!r}")

        gas_units = _SWAP_GAS_UNITS.get(chain, 180_000) * multiplier
        gas_cost_native = (gas_price_wei * gas_units) / 1e18
        return GasEstimate(chain, gas_cost_native, gas_cost_native * native_token_price_usd, complexity)
