"""Chain Execution Model (Multi-Market Opportunity Engine, V5.5, spec section 15).

A DEX transaction's execution timeline is fundamentally different from a
CEX taker order (near-instant, exchange-matched): it must be broadcast,
sit briefly in a mempool (or be forwarded to a leader), and wait for block
inclusion before it's final — and that timeline is chain-specific.
Ethereum's ~12s blocks and Solana's ~450ms slots are not the same
execution reality; reusing one EVM-shaped assumption for Solana would be
wrong by more than an order of magnitude (spec section 15's own
instruction not to do that). ChainExecutionModel is the shared interface;
EVMExecutionModel and SolanaExecutionModel are the two concrete,
chain-specific timelines.

expected_opportunity_lifetime_seconds is a conservative, DOCUMENTED
assumption, not a measurement — no real historical block-by-block replay
data exists yet to calibrate it against (section 24's Replay is the real
future source for that). Section 14's own rule — reject when expected
inclusion time exceeds the opportunity's expected lifetime — is
implemented exactly as literally stated, using this documented value.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class InclusionEstimate:
    chain: str
    broadcast_latency_seconds: float
    mempool_latency_seconds: float
    block_inclusion_latency_seconds: float
    confirmation_latency_seconds: float

    @property
    def total_seconds(self) -> float:
        return (
            self.broadcast_latency_seconds
            + self.mempool_latency_seconds
            + self.block_inclusion_latency_seconds
            + self.confirmation_latency_seconds
        )


class ChainExecutionModel(ABC):
    chain: str

    @abstractmethod
    def estimate_inclusion(self) -> InclusionEstimate: ...

    @abstractmethod
    def expected_opportunity_lifetime_seconds(self) -> float: ...

    def is_capturable(self) -> bool:
        """Spec section 14: "If expected_inclusion_time > expected_opportunity_lifetime,
        reject" — implemented exactly as stated."""
        return self.estimate_inclusion().total_seconds <= self.expected_opportunity_lifetime_seconds()


class EVMExecutionModel(ChainExecutionModel):
    # Real, publicly documented average block times.
    _BLOCK_TIME_SECONDS = {"eth": 12.0, "bsc": 3.0}
    # How many blocks a transaction realistically needs at a competitive
    # (not maximal) priority fee — 1 is optimistic-but-common on an
    # uncongested network; a conservative default, not a live estimate
    # (a real per-transaction estimate needs mempool congestion data this
    # module doesn't have — flagged as future work, not fabricated here).
    _BLOCKS_TO_INCLUSION = 1.0
    # A liquid, actively-arbed EVM pool's price gap typically survives on
    # the order of one to a few blocks before a competing searcher or
    # organic flow closes it — documented, conservative, NOT measured.
    _EXPECTED_LIFETIME_SECONDS = {"eth": 24.0, "bsc": 9.0}

    def __init__(self, chain: str) -> None:
        self.chain = chain

    def estimate_inclusion(self) -> InclusionEstimate:
        block_time = self._BLOCK_TIME_SECONDS.get(self.chain, 12.0)
        return InclusionEstimate(
            chain=self.chain,
            broadcast_latency_seconds=0.5,
            mempool_latency_seconds=0.5,
            block_inclusion_latency_seconds=block_time * self._BLOCKS_TO_INCLUSION,
            # A modest half-block buffer, not a second full block — a
            # searcher's transaction result is known the moment it's
            # included, not after waiting out another entire block; this
            # models a small residual margin, not literal reorg-safe finality.
            confirmation_latency_seconds=block_time * 0.5,
        )

    def expected_opportunity_lifetime_seconds(self) -> float:
        return self._EXPECTED_LIFETIME_SECONDS.get(self.chain, 12.0)


class SolanaExecutionModel(ChainExecutionModel):
    chain = "solana"
    _SLOT_TIME_SECONDS = 0.45
    _SLOTS_TO_CONFIRMATION = 2.0  # Solana's own "confirmed" commitment level
    # Solana's MEV/searcher ecosystem is well documented as extremely fast
    # relative to its own already-fast block time — a genuine cross-DEX gap
    # there is expected to close quicker, in absolute terms, than on a
    # slower EVM chain. Documented, conservative, NOT measured.
    _EXPECTED_LIFETIME_SECONDS = 2.5

    def estimate_inclusion(self) -> InclusionEstimate:
        return InclusionEstimate(
            chain="solana",
            broadcast_latency_seconds=0.1,
            # Solana has no traditional public mempool (transactions are
            # forwarded directly to the current/next leader) — a small
            # assumed forwarding delay stands in for it.
            mempool_latency_seconds=0.05,
            block_inclusion_latency_seconds=self._SLOT_TIME_SECONDS,
            confirmation_latency_seconds=self._SLOT_TIME_SECONDS * self._SLOTS_TO_CONFIRMATION,
        )

    def expected_opportunity_lifetime_seconds(self) -> float:
        return self._EXPECTED_LIFETIME_SECONDS


def build_execution_model(chain: str) -> ChainExecutionModel:
    if chain == "solana":
        return SolanaExecutionModel()
    return EVMExecutionModel(chain)
