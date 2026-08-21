"""Fee Engine (section 11; pair overrides + advantage detection are Reality Engine spec sections 12-13)."""

from app.config.constants import MarketType
from app.config.fees import DEFAULT_FEE_SCHEDULES, PAIR_FEE_OVERRIDES, ExchangeFeeSchedule, PairFeeOverride


class FeeEngine:
    def __init__(
        self,
        schedules: dict[str, ExchangeFeeSchedule] = DEFAULT_FEE_SCHEDULES,
        pair_overrides: dict[tuple[str, str], PairFeeOverride] = PAIR_FEE_OVERRIDES,
    ) -> None:
        self.schedules = schedules
        self.pair_overrides = pair_overrides

    def trading_fee(
        self, exchange: str, market: MarketType, notional_usd: float, *, is_maker: bool, symbol: str | None = None
    ) -> float:
        return notional_usd * self.effective_rate(exchange, market, is_maker=is_maker, symbol=symbol)

    def effective_rate(self, exchange: str, market: MarketType, *, is_maker: bool, symbol: str | None = None) -> float:
        """The rate actually applied — a pair-specific override (section
        12's "Special Pair Fees") when one exists and covers this side,
        otherwise the exchange's standard rate for that market/maker-taker
        combination."""
        schedule = self.schedules[exchange]
        if market == MarketType.SPOT:
            default_rate = schedule.maker_fee_spot if is_maker else schedule.taker_fee_spot
        else:
            default_rate = schedule.maker_fee_futures if is_maker else schedule.taker_fee_futures

        if market == MarketType.SPOT and symbol is not None:
            override = self.pair_overrides.get((exchange, symbol))
            if override is not None:
                override_rate = override.maker_fee_spot if is_maker else override.taker_fee_spot
                if override_rate is not None:
                    return override_rate
        return default_rate

    def withdrawal_fee(self, exchange: str) -> float:
        return self.schedules[exchange].withdrawal_fee_usd


def find_fee_advantaged_routes(
    fee_engine: FeeEngine, exchange: str, candidate_symbols: list[str], *, is_maker: bool = False
) -> list[tuple[str, float]]:
    """Reality Engine spec, section 13 — "FEE ADVANTAGE DETECTOR": ranks
    candidate pairs by their effective taker (or maker) fee rate, lowest
    first, so a strategy with a choice of routes (e.g. which stablecoin to
    bridge through) can see whether one carries a pair-specific promotion
    over the exchange's standard rate. Returns (symbol, rate_pct) pairs;
    with PAIR_FEE_OVERRIDES empty this just reports the standard rate for
    each — the detector has nothing to detect until a real, verified
    promotion is populated there."""
    ranked = [
        (symbol, fee_engine.effective_rate(exchange, MarketType.SPOT, is_maker=is_maker, symbol=symbol) * 100)
        for symbol in candidate_symbols
    ]
    return sorted(ranked, key=lambda pair: pair[1])
