"""End-to-end check: detector -> classification/score -> paper trading, no network/DB."""

import time

import pytest

from app.config.constants import MarketType, OpportunityClassification
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.market_data.normalizer import NormalizedQuote
from app.market_data.store import MarketDataStore
from app.opportunity.detector import OpportunityDetector
from app.simulation.money import round_usd
from app.simulation.paper_trader import EXECUTION_SLIPPAGE_MEAN_PCT, PaperTrader, TradeStatus
from app.simulation.portfolios import build_default_portfolios


class _DeterministicRng:
    """Never trips a rare-event `< probability` check; `.gauss()` always
    returns exactly the mean so exact profit assertions still hold."""

    def random(self) -> float:
        return 1.0

    def gauss(self, mu: float, sigma: float) -> float:
        return mu

    def uniform(self, low: float, high: float) -> float:
        return low


def make_quote(exchange: str, symbol: str, bid: float, ask: float, qty: float = 10.0) -> NormalizedQuote:
    now = time.time()
    return NormalizedQuote(
        exchange=exchange, market=MarketType.SPOT, symbol=symbol,
        bid=bid, ask=ask, bid_quantity=qty, ask_quantity=qty,
        exchange_timestamp=now, received_at=now,
    )


@pytest.mark.asyncio
async def test_full_pipeline_detect_score_and_paper_trade():
    store = MarketDataStore()
    store.update_quote(make_quote("binance", "BTC/USDT", bid=99_990, ask=100_000))
    store.update_quote(make_quote("okx", "BTC/USDT", bid=100_800, ask=100_810))

    detector = OpportunityDetector([CrossExchangeArbitrageEngine(assets=["BTC"], store=store, capital_usd=1_000)])
    opportunities = await detector.scan_once()

    assert len(opportunities) == 1
    opp = opportunities[0]
    assert opp.classification in OpportunityClassification
    assert 0 <= opp.score <= 100

    portfolios = build_default_portfolios()
    portfolio = next(p for p in portfolios if p.name == "1K")
    paper_trader = PaperTrader(rng=_DeterministicRng())
    outcome = paper_trader.determine_outcome(opp)
    assert outcome == TradeStatus.SIMULATED_EXECUTED  # large, fresh, fully-sized taker/taker spread
    trade = paper_trader.simulate(opp, portfolio, outcome)

    # Capped at 20% of the $1,000 portfolio (default risk limit, spec section
    # 31) — well under the $1,000 the opportunity itself was priced/liquidity-capped at.
    assert trade.capital_usd == pytest.approx(200.0)
    expected_scale = 200.0 / opp.capital_usd
    expected_profit = round_usd(opp.expected_profit_usd * expected_scale + 200.0 * (EXECUTION_SLIPPAGE_MEAN_PCT / 100))
    assert trade.net_profit_usd == pytest.approx(expected_profit)
    assert portfolio.balances["USDT"] == pytest.approx(1_000 + trade.net_profit_usd)
