"""Exchange Client interface (Reality Engine spec, sections 57-58 —
Binance Testnet preparation).

Defines the contract a future real exchange connection (testnet first,
live much later) would fulfill, without implementing real order placement
yet — the same "prepare the interface, don't mix it with simulation"
posture already taken for DEX/on-chain prep (spec section 54). Nothing in
the detection loop, engines, or PaperTrader depends on this or is touched
by it; it exists purely so a future step has somewhere to plug in.

NO REAL MONEY EXECUTION remains the hard rule established across every
spec this system has been built from — no implementation of this
interface in this codebase places a real order.
"""

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(slots=True)
class ExchangeConnectivity:
    reachable: bool  # the exchange's API answered at all (public endpoint, no auth needed)
    credentials_configured: bool  # an API key/secret is present in settings — NOT the same as "authenticated"
    latency_ms: float | None
    detail: str


class ExchangeClient(ABC):
    @abstractmethod
    async def check_connectivity(self) -> ExchangeConnectivity: ...
