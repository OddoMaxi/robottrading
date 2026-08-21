"""On-chain data model (Multi-Market Opportunity Engine, V5.5).

DexPool is the on-chain analogue of app.market_data.normalizer.NormalizedQuote
— what every DEX/network payload gets converted into before anything else
touches it. Deliberately its own type rather than reusing CEX's
NormalizedQuote: a pool has no separate bid/ask (an AMM's price is a
function of its reserves and trade size, not a resting order book), and
carries on-chain-only concepts (chain, TVL, pool age) a CEX quote has no
equivalent for.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class DexPool:
    chain: str  # "eth", "bsc", "solana"
    dex: str  # "uniswap_v3", "pancakeswap-v3-bsc", "raydium", "orca"
    pool_id: str  # the DEX's own pool address/id
    token0_symbol: str
    token1_symbol: str
    price: float  # token0 price, quoted in token1 (GeckoTerminal's base/quote convention)
    tvl_usd: float
    volume_24h_usd: float
    fee_pct: float  # swap fee, e.g. 0.30 for 0.30% — extracted where possible, else a documented per-DEX default
    pool_created_at: datetime | None
    last_update: float  # epoch seconds, local fetch time

    @property
    def age_hours(self) -> float | None:
        if self.pool_created_at is None:
            return None
        from datetime import UTC

        now = datetime.now(UTC).replace(tzinfo=None)
        created = self.pool_created_at.replace(tzinfo=None) if self.pool_created_at.tzinfo else self.pool_created_at
        return (now - created).total_seconds() / 3600.0
