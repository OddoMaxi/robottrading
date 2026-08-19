"""Default fee schedules per exchange (section 11 — Fee Engine).

Placeholder values (standard non-VIP retail tiers). Must be verified against
each exchange's live fee schedule before any results are trusted — VIP tiers
and promotional discounts are not reflected here yet.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ExchangeFeeSchedule:
    maker_fee_spot: float
    taker_fee_spot: float
    maker_fee_futures: float
    taker_fee_futures: float
    withdrawal_fee_usd: float = 0.0
    vip_level: int = 0


DEFAULT_FEE_SCHEDULES: dict[str, ExchangeFeeSchedule] = {
    "binance": ExchangeFeeSchedule(
        maker_fee_spot=0.0010,
        taker_fee_spot=0.0010,
        maker_fee_futures=0.0002,
        taker_fee_futures=0.0005,
    ),
    "okx": ExchangeFeeSchedule(
        maker_fee_spot=0.0008,
        taker_fee_spot=0.0010,
        maker_fee_futures=0.0002,
        taker_fee_futures=0.0005,
    ),
    "bybit": ExchangeFeeSchedule(
        maker_fee_spot=0.0010,
        taker_fee_spot=0.0010,
        maker_fee_futures=0.0002,
        taker_fee_futures=0.0006,
    ),
}
