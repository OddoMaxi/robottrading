"""Fee Engine (section 11)."""

from app.config.constants import MarketType
from app.config.fees import DEFAULT_FEE_SCHEDULES, ExchangeFeeSchedule


class FeeEngine:
    def __init__(self, schedules: dict[str, ExchangeFeeSchedule] = DEFAULT_FEE_SCHEDULES) -> None:
        self.schedules = schedules

    def trading_fee(self, exchange: str, market: MarketType, notional_usd: float, *, is_maker: bool) -> float:
        schedule = self.schedules[exchange]
        if market == MarketType.SPOT:
            rate = schedule.maker_fee_spot if is_maker else schedule.taker_fee_spot
        else:
            rate = schedule.maker_fee_futures if is_maker else schedule.taker_fee_futures
        return notional_usd * rate

    def withdrawal_fee(self, exchange: str) -> float:
        return self.schedules[exchange].withdrawal_fee_usd
