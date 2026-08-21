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


@dataclass(frozen=True)
class PairFeeOverride:
    """A symbol-specific rate that replaces the exchange's default for that
    one pair — e.g. Binance's periodic 0-fee FDUSD promotions. `None` on
    either field means "no override, use the exchange's standard rate."""

    maker_fee_spot: float | None = None
    taker_fee_spot: float | None = None
    note: str = ""


# Reality Engine spec, sections 12-13 — pair-specific fee overrides
# ("Special Pair Fees... Fee Promotions... FDUSD Promotions"). Deliberately
# empty: no public API exposes an account's live promotional fee status,
# and Binance's FDUSD promotions have historically started, expired, and
# changed scope without notice. Hardcoding a rate we can't verify right now
# would silently make results look better than reality — the opposite of
# this spec's own rule (section 87: never tune a number just to get a
# better result). Populate an entry here only after checking
# https://www.binance.com/fee/schedule (or the account's own fee page) for
# whichever promotion is actually live, e.g.:
#   ("binance", "BTC/FDUSD"): PairFeeOverride(maker_fee_spot=0.0, taker_fee_spot=0.0, note="verified <date>")
PAIR_FEE_OVERRIDES: dict[tuple[str, str], PairFeeOverride] = {}


def uniform_fee_schedules(maker_fee_spot: float, taker_fee_spot: float) -> dict[str, ExchangeFeeSchedule]:
    """Build a fee schedule with the same maker/taker spot rate on every exchange.

    For "what if I had a lower VIP tier" exploration: real published VIP fee
    tables differ per exchange and change over time, and we have no way to
    fetch a specific account's actual tier from a public endpoint (that's
    authenticated, account-specific data). Rather than hard-coding VIP tier
    numbers that could be stale or wrong, this lets the caller plug in
    whatever rate their exchange account page actually shows.
    """
    return {
        exchange: ExchangeFeeSchedule(
            maker_fee_spot=maker_fee_spot,
            taker_fee_spot=taker_fee_spot,
            maker_fee_futures=schedule.maker_fee_futures,
            taker_fee_futures=schedule.taker_fee_futures,
        )
        for exchange, schedule in DEFAULT_FEE_SCHEDULES.items()
    }


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
