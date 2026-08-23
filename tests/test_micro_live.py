import uuid

from app.config.constants import Strategy
from app.execution.binance_account_client import BinanceBalance, BinanceCredentialsMissing
from app.execution.micro_live import MicroLiveOrchestrator, MicroLiveState, _binance_leg, _binance_symbol
from app.opportunity.models import Opportunity

EXCHANGE_INFO_FIXTURE = {
    "symbols": [
        {
            "symbol": "BTCUSDT",
            "status": "TRADING",
            "baseAsset": "BTC",
            "quoteAsset": "USDT",
            "baseAssetPrecision": 8,
            "quoteAssetPrecision": 8,
            "orderTypes": ["LIMIT", "MARKET"],
            "isSpotTradingAllowed": True,
            "filters": [
                {"filterType": "PRICE_FILTER", "minPrice": "0.01", "maxPrice": "1000000.00", "tickSize": "0.01"},
                {"filterType": "LOT_SIZE", "minQty": "0.00001", "maxQty": "9000.0", "stepSize": "0.00001"},
                {"filterType": "NOTIONAL", "minNotional": "5.00", "applyMinToMarket": True},
            ],
        }
    ]
}


def _opp(symbol="BTC/USDT", side="BUY", exchange="binance", capital_usd=500.0, gross_spread_pct=1.0) -> Opportunity:
    return Opportunity(
        strategy=Strategy.CROSS_EXCHANGE,
        symbol=symbol,
        legs=[
            {"exchange": exchange, "side": side, "market": "spot", "price": 50_000.0, "quantity": 0.01},
            {"exchange": "okx", "side": "SELL" if side == "BUY" else "BUY", "market": "spot", "price": 50_100.0, "quantity": 0.01},
        ],
        gross_spread_pct=gross_spread_pct,
        capital_usd=capital_usd,
        id=uuid.uuid4(),
    )


def test_binance_symbol_strips_slash():
    assert _binance_symbol("BTC/USDT") == "BTCUSDT"


def test_binance_leg_finds_binance_exchange():
    opp = _opp()
    leg = _binance_leg(opp)
    assert leg is not None
    assert leg["exchange"] == "binance"


def test_binance_leg_none_when_no_binance_leg_present():
    opp = _opp(exchange="okx")
    assert _binance_leg(opp) is None


class FakeClient:
    def __init__(self, credentials_missing=False):
        self.credentials_missing = credentials_missing

    async def get_account_snapshot(self):
        if self.credentials_missing:
            raise BinanceCredentialsMissing("no creds")
        return type(
            "Snap",
            (),
            {"balances": [BinanceBalance(asset="USDT", free=42.0, locked=0.0)], "balance_usdt": lambda self=None: 42.0},
        )()

    async def get_exchange_info(self, symbols=None):
        return EXCHANGE_INFO_FIXTURE

    async def get_book_ticker(self, symbol):
        return {"bidPrice": "50000.0", "askPrice": "50010.0"}

    async def get_order_book_depth(self, symbol, limit=20):
        return {"asks": [["50010.0", "1.0"], ["50020.0", "1.0"]], "bids": [["50000.0", "1.0"]]}


async def test_observe_reality_quote_skips_opportunities_without_binance_leg():
    orchestrator = MicroLiveOrchestrator(client=FakeClient())
    quote = await orchestrator.observe_reality_quote(_opp(exchange="okx"))
    assert quote is None


async def test_observe_reality_quote_returns_quote_for_binance_leg_opportunity():
    orchestrator = MicroLiveOrchestrator(client=FakeClient())
    quote = await orchestrator.observe_reality_quote(_opp())
    assert quote is not None
    assert quote.symbol == "BTCUSDT"
    assert quote.best_bid == 50_000.0
    assert quote.best_ask == 50_010.0


async def test_observe_reality_quote_handles_missing_credentials_gracefully():
    """Missing credentials must degrade to 'no observation', never raise
    into the CEX detection loop."""
    from app.execution import micro_live as micro_live_module

    orchestrator = MicroLiveOrchestrator(client=FakeClient(credentials_missing=True))
    quote = await orchestrator.observe_reality_quote(_opp())
    assert quote is not None  # balance defaults to 0.0, quote is still computed against real book/filters
    assert micro_live_module.micro_live_state.account_snapshot_error == "no creds"


def test_summary_buckets_rejection_reasons():
    from app.execution.reality_quote import RealityQuote

    state = MicroLiveState()
    state.record(
        RealityQuote(
            opportunity_id=uuid.uuid4(), symbol="BTCUSDT", side="BUY",
            master_requested_size_usd=10.0, exchange_valid_size_usd=3.0,
            best_bid=50_000.0, best_ask=50_010.0, available_depth_usd=1000.0,
            estimated_fees_usd=0.003, estimated_slippage_pct=0.01,
            estimated_net_profit_after_real_constraints_usd=-0.5,
            min_notional_pass=False, lot_size_pass=True, balance_pass=True,
            executable=False, reason="notional too small", fee_source="estimated_default", computed_at=0.0,
        ),
        strategy="cross_exchange",
    )
    summary = state.summary()
    assert summary["opportunities_observed"] == 1
    assert summary["non_executable"] == 1
    assert summary["rejection_reasons"] == {"min_notional": 1}
