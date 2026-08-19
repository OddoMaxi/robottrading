"""False Opportunity Filter (Net Opportunity Engine spec, section 18).

Guards against acting on stale or desynchronized market data. A "spread"
built from two quotes that arrived seconds apart, or from a quote that's
gone stale since the last tick, isn't a real simultaneous price gap — it's
a data artifact. Never paper-trade on data this check rejects.
"""

from dataclasses import dataclass

from app.market_data.normalizer import NormalizedQuote

# A quote is stale once it's this old relative to "now" — collectors push
# updates on every tick, so anything older than this means the feed likely
# stalled (reconnect in progress, exchange outage) rather than the market
# just being quiet.
MAX_QUOTE_AGE_SECONDS = 2.0

# Max allowed gap between the two legs' own data ages. A big skew means
# we're comparing a fresh quote against a comparatively old one — not a
# real, simultaneous cross-exchange spread. Deliberately smaller than
# MAX_QUOTE_AGE_SECONDS: two quotes can each individually pass the
# freshness check yet still be too far apart from each other in age.
MAX_INTER_LEG_AGE_SKEW_SECONDS = 1.0


@dataclass(slots=True)
class DataQualityCheck:
    is_valid: bool
    reason: str | None
    market_data_age_seconds: float


def check_quote_freshness(quote: NormalizedQuote, now: float) -> DataQualityCheck:
    age = max(0.0, now - quote.received_at)
    if age > MAX_QUOTE_AGE_SECONDS:
        return DataQualityCheck(False, f"stale data ({age:.1f}s old)", age)
    return DataQualityCheck(True, None, age)


def check_leg_pair_sync(buy_quote: NormalizedQuote, sell_quote: NormalizedQuote, now: float) -> DataQualityCheck:
    buy_check = check_quote_freshness(buy_quote, now)
    sell_check = check_quote_freshness(sell_quote, now)
    max_age = max(buy_check.market_data_age_seconds, sell_check.market_data_age_seconds)

    if not buy_check.is_valid:
        return DataQualityCheck(False, buy_check.reason, max_age)
    if not sell_check.is_valid:
        return DataQualityCheck(False, sell_check.reason, max_age)

    skew = abs(buy_check.market_data_age_seconds - sell_check.market_data_age_seconds)
    if skew > MAX_INTER_LEG_AGE_SKEW_SECONDS:
        return DataQualityCheck(False, f"desynced quotes (skew {skew:.1f}s)", max_age)

    return DataQualityCheck(True, None, max_age)
