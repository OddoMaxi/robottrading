import uuid
from dataclasses import replace

from app.execution.dual_leg_quote import DualLegQuote
from app.execution.live_ranker import check_prepositioning, rank_live_opportunities, score_opportunity


def _quote(**overrides) -> DualLegQuote:
    base = dict(
        opportunity_id=uuid.uuid4(),
        symbol="ZRO/USDT",
        buy_exchange="binance",
        sell_exchange="bybit",
        buy_execution_price=3.10,
        sell_execution_price=3.16,
        executable_qty=1.6,
        buy_valid_qty=1.6,
        sell_valid_qty=1.6,
        gross_spread_pct=1.9,
        buy_fee_usd=0.005,
        sell_fee_usd=0.005,
        buy_slippage_pct=0.01,
        sell_slippage_pct=0.01,
        buy_quote_age_ms=10.0,
        sell_quote_age_ms=10.0,
        dual_leg_latency_ms=200.0,
        net_profit_usd=0.08,
        net_return_bps=161.0,
        buy_min_notional_pass=True,
        buy_lot_size_pass=True,
        sell_min_notional_pass=True,
        sell_lot_size_pass=True,
        buy_tradable=True,
        sell_tradable=True,
        executable=True,
        reason=None,
        buy_fee_source="real_account_fee",
        sell_fee_source="real_account_fee",
        computed_at=100.0,
    )
    base.update(overrides)
    return DualLegQuote(**base)


def test_prepositioning_pass_when_balances_sufficient():
    quote = _quote()
    check = check_prepositioning(
        quote, "binance", "bybit", "ZRO",
        available_usdt={"binance": 10.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 2.0},
    )
    assert check.prepositioned is True
    assert check.executable_now is True
    assert check.required_buy_balance_usdt == 1.6 * 3.10


def test_prepositioning_fails_when_buy_balance_insufficient():
    quote = _quote()
    check = check_prepositioning(
        quote, "binance", "bybit", "ZRO",
        available_usdt={"binance": 1.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 2.0},
    )
    assert check.prepositioned is False
    assert check.executable_now is False


def test_prepositioning_fails_when_sell_asset_missing():
    quote = _quote()
    check = check_prepositioning(
        quote, "binance", "bybit", "ZRO",
        available_usdt={"binance": 10.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 0.0},  # no ZRO on Bybit
    )
    assert check.prepositioned is False
    assert check.executable_now is False


def test_prepositioning_uses_the_right_exchange_for_each_leg():
    """Binance is the SELL exchange here — must check binance_base, not bybit_base."""
    quote = _quote(buy_exchange="bybit", sell_exchange="binance")
    check = check_prepositioning(
        quote, "bybit", "binance", "ZRO",
        available_usdt={"binance": 0.0, "bybit": 10.0}, available_base={"binance": 2.0, "bybit": 0.0},
    )
    assert check.prepositioned is True


def test_score_zero_when_not_prepositioned():
    quote = _quote()
    check = check_prepositioning(quote, "binance", "bybit", "ZRO", available_usdt={"binance": 0.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 0.0})
    assert score_opportunity(quote, check) == 0.0


def test_score_zero_when_net_profit_not_positive():
    quote = _quote(net_profit_usd=-0.01)
    check = check_prepositioning(quote, "binance", "bybit", "ZRO", available_usdt={"binance": 10.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 2.0})
    assert score_opportunity(quote, check) == 0.0


def test_score_positive_when_qualified():
    quote = _quote()
    check = check_prepositioning(quote, "binance", "bybit", "ZRO", available_usdt={"binance": 10.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 2.0})
    assert score_opportunity(quote, check) > 0.0


def test_higher_latency_scores_lower_for_the_same_profit():
    quote_fast = _quote(dual_leg_latency_ms=50.0)
    quote_slow = _quote(dual_leg_latency_ms=5000.0)
    check = check_prepositioning(quote_fast, "binance", "bybit", "ZRO", available_usdt={"binance": 10.0, "bybit": 0.0}, available_base={"binance": 0.0, "bybit": 2.0})
    assert score_opportunity(quote_fast, check) > score_opportunity(quote_slow, check)


class FakeUniverseBuilder:
    async def get_universe(self, force_refresh=False):
        from app.execution.live_universe import LiveUniverse

        return LiveUniverse(common_symbols=["ZRO/USDT"], binance_okx_symbols=[], bybit_okx_symbols=[], all_three_symbols=[], binance_symbol_count=1, bybit_symbol_count=1, okx_symbol_count=0, fetched_at=100.0)


class FakeBinanceRead:
    async def get_account_snapshot(self):
        return type("Snap", (), {"balance_usdt": lambda self=None: 10.0, "balance_of": lambda self, asset: 0.0})()


class FakeBybitRead:
    async def get_wallet_balance(self):
        return {"result": {"list": [{"coin": [{"coin": "USDT", "availableToWithdraw": "0"}, {"coin": "ZRO", "availableToWithdraw": "5"}]}]}}


class FakeOkxRead:
    async def get_account_snapshot(self):
        return type("Snap", (), {"balance_usdt": lambda self=None: 0.0, "balance_of": lambda self, asset: 0.0})()


class FakeFetcher:
    async def fetch(self, exchange, symbol):
        from app.scanner.market_snapshot import SymbolMarketData

        if exchange == "binance":
            return SymbolMarketData(
                exchange="binance", symbol=symbol, best_bid=3.09, best_ask=3.10,
                ask_depth=[(3.10, 100.0)], bid_depth=[(3.09, 100.0)],
                min_qty=0.01, step_size=0.01, tick_size=0.0001, min_notional=5.0,
                tradable=True, maker_fee_rate=0.001, taker_fee_rate=0.001, fee_source="real_account_fee", fetched_at=100.0,
            )
        if exchange == "bybit":
            return SymbolMarketData(
                exchange="bybit", symbol=symbol, best_bid=3.16, best_ask=3.17,
                ask_depth=[(3.17, 100.0)], bid_depth=[(3.16, 100.0)],
                min_qty=0.01, step_size=0.01, tick_size=0.0001, min_notional=1.0,
                tradable=True, maker_fee_rate=0.001, taker_fee_rate=0.001, fee_source="real_account_fee", fetched_at=100.0,
            )
        return None


async def test_rank_live_opportunities_end_to_end_with_real_balances():
    ranked = await rank_live_opportunities(
        universe_builder=FakeUniverseBuilder(),
        binance_read=FakeBinanceRead(),
        bybit_read=FakeBybitRead(),
        okx_read=FakeOkxRead(),
        fetcher=FakeFetcher(),
        requested_notional_per_leg_usdt=6.0,  # comfortably above Binance's 5.0 min_notional after step-size rounding
    )
    assert len(ranked) == 2  # binance->bybit and bybit->binance
    winner = ranked[0]
    assert winner.symbol == "ZRO/USDT"
    assert winner.buy_exchange == "binance"
    assert winner.sell_exchange == "bybit"
    assert winner.prepositioning.executable_now is True
    assert winner.score > 0


class FakeFetcherWithOkx(FakeFetcher):
    async def fetch(self, exchange, symbol):
        from app.scanner.market_snapshot import SymbolMarketData

        if exchange == "okx":
            return SymbolMarketData(
                exchange="okx", symbol=symbol, best_bid=3.20, best_ask=3.21,
                ask_depth=[(3.21, 100.0)], bid_depth=[(3.20, 100.0)],
                min_qty=0.01, step_size=0.01, tick_size=0.0001, min_notional=None,
                tradable=True, maker_fee_rate=0.0008, taker_fee_rate=0.001, fee_source="real_account_fee", fetched_at=100.0,
            )
        return await super().fetch(exchange, symbol)


async def test_rank_live_opportunities_includes_okx_directions_and_prepositions_against_real_okx_balance():
    """OKX must be scanned and ranked exactly like Binance/Bybit --
    binance->okx (buy at 3.10, sell at OKX's 3.20 bid) is the best
    direction here, and must only be executable_now once OKX itself
    reports enough USDT/inventory."""

    class FakeOkxReadFunded:
        async def get_account_snapshot(self):
            return type("Snap", (), {"balance_usdt": lambda self=None: 10.0, "balance_of": lambda self, asset: 5.0})()

    ranked = await rank_live_opportunities(
        universe_builder=FakeUniverseBuilder(),
        binance_read=FakeBinanceRead(),
        bybit_read=FakeBybitRead(),
        okx_read=FakeOkxReadFunded(),
        fetcher=FakeFetcherWithOkx(),
        requested_notional_per_leg_usdt=6.0,
    )
    okx_directions = [r for r in ranked if "okx" in (r.buy_exchange, r.sell_exchange)]
    assert len(okx_directions) > 0
    winner = ranked[0]
    assert "okx" in (winner.buy_exchange, winner.sell_exchange)
    assert winner.prepositioning.executable_now is True


async def test_rank_live_opportunities_okx_direction_blocked_when_okx_unfunded():
    """The same OKX-involving opportunity, but with OKX genuinely
    unfunded (balance_usdt=0.0, matching the real account today) --
    executable_now must be False, never silently treated as available."""
    ranked = await rank_live_opportunities(
        universe_builder=FakeUniverseBuilder(),
        binance_read=FakeBinanceRead(),
        bybit_read=FakeBybitRead(),
        okx_read=FakeOkxRead(),  # balance_usdt=0.0, balance_of=0.0
        fetcher=FakeFetcherWithOkx(),
        requested_notional_per_leg_usdt=6.0,
    )
    okx_as_buy = [r for r in ranked if r.buy_exchange == "okx"]
    assert okx_as_buy
    assert all(r.prepositioning.executable_now is False for r in okx_as_buy)
