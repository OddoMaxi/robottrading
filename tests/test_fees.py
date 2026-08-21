from app.analytics.fees import FeeEngine, find_fee_advantaged_routes
from app.config.constants import MarketType
from app.config.fees import DEFAULT_FEE_SCHEDULES, ExchangeFeeSchedule, PairFeeOverride


def test_trading_fee_uses_the_exchanges_standard_rate_by_default():
    engine = FeeEngine()
    fee = engine.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=False)
    assert fee == 1_000.0 * DEFAULT_FEE_SCHEDULES["binance"].taker_fee_spot


def test_trading_fee_with_no_symbol_ignores_any_pair_overrides():
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0)}
    engine = FeeEngine(pair_overrides=overrides)
    fee = engine.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=False)  # no symbol passed
    assert fee == 1_000.0 * DEFAULT_FEE_SCHEDULES["binance"].taker_fee_spot


def test_pair_override_replaces_the_standard_rate_when_symbol_matches():
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0, maker_fee_spot=0.0)}
    engine = FeeEngine(pair_overrides=overrides)
    fee = engine.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=False, symbol="BTC/FDUSD")
    assert fee == 0.0


def test_pair_override_only_applies_to_its_own_symbol():
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0)}
    engine = FeeEngine(pair_overrides=overrides)
    fee = engine.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=False, symbol="ETH/USDT")
    assert fee == 1_000.0 * DEFAULT_FEE_SCHEDULES["binance"].taker_fee_spot


def test_pair_override_only_applies_to_the_exchange_it_was_set_for():
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0)}
    engine = FeeEngine(pair_overrides=overrides)
    fee = engine.trading_fee("okx", MarketType.SPOT, 1_000.0, is_maker=False, symbol="BTC/FDUSD")
    assert fee == 1_000.0 * DEFAULT_FEE_SCHEDULES["okx"].taker_fee_spot


def test_pair_override_never_applies_to_futures_market():
    """A spot-pair fee promotion has no bearing on a futures/perpetual leg."""
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0)}
    engine = FeeEngine(schedules={"binance": ExchangeFeeSchedule(0.001, 0.001, 0.0002, 0.0005)}, pair_overrides=overrides)
    fee = engine.trading_fee("binance", MarketType.FUTURES, 1_000.0, is_maker=False, symbol="BTC/FDUSD")
    assert fee == 1_000.0 * 0.0005


def test_partial_override_only_replaces_the_side_it_sets():
    """maker_fee_spot=None in the override means "no maker override" even
    though a taker override exists for the same pair."""
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0, maker_fee_spot=None)}
    engine = FeeEngine(pair_overrides=overrides)
    maker_fee = engine.trading_fee("binance", MarketType.SPOT, 1_000.0, is_maker=True, symbol="BTC/FDUSD")
    assert maker_fee == 1_000.0 * DEFAULT_FEE_SCHEDULES["binance"].maker_fee_spot


def test_find_fee_advantaged_routes_ranks_lowest_effective_rate_first():
    overrides = {("binance", "BTC/FDUSD"): PairFeeOverride(taker_fee_spot=0.0)}
    engine = FeeEngine(pair_overrides=overrides)
    ranked = find_fee_advantaged_routes(engine, "binance", ["BTC/USDT", "BTC/FDUSD", "BTC/USDC"])
    assert ranked[0][0] == "BTC/FDUSD"
    assert ranked[0][1] == 0.0


def test_find_fee_advantaged_routes_with_no_overrides_reports_the_standard_rate_for_all():
    engine = FeeEngine()
    ranked = find_fee_advantaged_routes(engine, "binance", ["BTC/USDT", "ETH/USDT"])
    standard_pct = DEFAULT_FEE_SCHEDULES["binance"].taker_fee_spot * 100
    assert all(rate == standard_pct for _, rate in ranked)
