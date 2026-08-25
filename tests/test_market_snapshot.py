from dataclasses import dataclass

import pytest

from app.execution.okx_account_client import OkxCredentialsMissing, OkxTradeFee
from app.scanner.market_snapshot import MultiExchangeSnapshotFetcher
from app.scanner.okx_public_client import OKX_ESTIMATED_MAKER_FEE_RATE, OKX_ESTIMATED_TAKER_FEE_RATE, OkxBookTicker, OkxSymbolRules


@dataclass
class _FakeOkxPublicClient:
    async def get_book_ticker(self, symbol: str):
        return OkxBookTicker(inst_id=symbol.replace("/", "-"), bid_price=0.003415, ask_price=0.003420)

    async def get_order_book_depth(self, symbol: str, limit: int = 20):
        return {"data": [{"asks": [["0.003420", "1000", "0", "1"]], "bids": [["0.003415", "1000", "0", "1"]]}]}

    async def get_symbol_rules(self, symbol: str):
        return OkxSymbolRules(inst_id=symbol.replace("/", "-"), is_tradable=True, min_qty=1.0, lot_size=1.0, tick_size=0.000001)


class _FakeOkxAccountClientWithFee:
    async def get_trade_fee(self, symbol: str):
        return OkxTradeFee(inst_id=symbol.replace("/", "-"), maker_fee_rate=0.0006, taker_fee_rate=0.0007)


class _FakeOkxAccountClientNoCredentials:
    async def get_trade_fee(self, symbol: str):
        raise OkxCredentialsMissing("okx_api_key/okx_api_secret/okx_api_passphrase not configured")


async def test_fetch_okx_uses_real_account_fee_when_available():
    fetcher = MultiExchangeSnapshotFetcher(okx=_FakeOkxPublicClient(), okx_account=_FakeOkxAccountClientWithFee())
    data = await fetcher.fetch("okx", "RVN/USDT")
    assert data is not None
    assert data.fee_source == "real_account_fee"
    assert data.taker_fee_rate == pytest.approx(0.0007)
    assert data.maker_fee_rate == pytest.approx(0.0006)


async def test_fetch_okx_falls_back_to_estimated_fee_without_credentials():
    fetcher = MultiExchangeSnapshotFetcher(okx=_FakeOkxPublicClient(), okx_account=_FakeOkxAccountClientNoCredentials())
    data = await fetcher.fetch("okx", "RVN/USDT")
    assert data is not None
    assert data.fee_source == "estimated_default"
    assert data.taker_fee_rate == pytest.approx(OKX_ESTIMATED_TAKER_FEE_RATE)
    assert data.maker_fee_rate == pytest.approx(OKX_ESTIMATED_MAKER_FEE_RATE)


async def test_fetch_okx_caches_fee_across_calls_within_ttl():
    calls = {"n": 0}

    class _CountingFeeClient:
        async def get_trade_fee(self, symbol: str):
            calls["n"] += 1
            return OkxTradeFee(inst_id=symbol.replace("/", "-"), maker_fee_rate=0.0006, taker_fee_rate=0.0007)

    fetcher = MultiExchangeSnapshotFetcher(okx=_FakeOkxPublicClient(), okx_account=_CountingFeeClient())
    await fetcher.fetch("okx", "RVN/USDT")
    await fetcher.fetch("okx", "RVN/USDT")
    assert calls["n"] == 1
