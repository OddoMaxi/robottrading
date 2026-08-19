"""Common interface for the four arbitrage engines (sections 4-7)."""

from abc import ABC, abstractmethod

from app.opportunity.models import Opportunity


class ArbitrageEngine(ABC):
    strategy_name: str

    @abstractmethod
    async def detect(self) -> list[Opportunity]:
        """Scan current market data and return newly detected opportunities.

        Implementations read from app.market_data.store.market_data_store,
        which the collectors populate.
        """
