from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.execution.okx_account_client import (
    OkxAccountClient,
    OkxCredentialsMissing,
    _parse_account_snapshot,
    _parse_trade_fee,
    okx_timestamp,
    sign_request,
    to_okx_symbol,
)


def test_to_okx_symbol_converts_slash_to_dash():
    assert to_okx_symbol("RVN/USDT") == "RVN-USDT"


def test_okx_timestamp_format():
    ts = okx_timestamp()
    assert ts.endswith("Z")
    assert "T" in ts
    assert len(ts) == 24  # "YYYY-MM-DDTHH:MM:SS.mmmZ"


def test_sign_request_is_deterministic_hmac_sha256_base64():
    sig1 = sign_request("secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/account/balance")
    sig2 = sign_request("secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/account/balance")
    assert sig1 == sig2
    assert sig1 != sign_request("different-secret", "2026-01-01T00:00:00.000Z", "GET", "/api/v5/account/balance")


BALANCE_FIXTURE = {
    "code": "0", "msg": "",
    "data": [{"details": [
        {"ccy": "USDT", "availBal": "34.99324042", "frozenBal": "0"},
        {"ccy": "RVN", "availBal": "6407.427", "frozenBal": "0"},
    ]}],
}

FEE_FIXTURE = {"code": "0", "data": [{"instType": "SPOT", "maker": "-0.0008", "taker": "-0.001"}]}


def test_parse_account_snapshot():
    snap = _parse_account_snapshot(BALANCE_FIXTURE)
    assert snap.balance_usdt() == pytest.approx(34.99324042)
    assert snap.balance_of("RVN") == pytest.approx(6407.427)
    assert snap.balance_of("UNKNOWN") == 0.0


def test_parse_trade_fee_normalizes_negative_rate_to_positive():
    fee = _parse_trade_fee(FEE_FIXTURE, "RVN-USDT")
    assert fee is not None
    assert fee.maker_fee_rate == pytest.approx(0.0008)
    assert fee.taker_fee_rate == pytest.approx(0.001)


def test_signed_headers_raise_without_full_credentials(monkeypatch):
    client = OkxAccountClient()
    monkeypatch.setattr("app.execution.okx_account_client.get_settings", lambda: SimpleNamespace(okx_api_key="", okx_api_secret="", okx_api_passphrase=""))
    with pytest.raises(OkxCredentialsMissing):
        client._signed_headers("GET", "/api/v5/account/balance")


def test_signed_headers_raise_when_passphrase_missing(monkeypatch):
    """A partially-configured key (key+secret but no passphrase) must
    still refuse to sign -- OKX's v5 API rejects any signed call missing
    OK-ACCESS-PASSPHRASE regardless of the other two headers."""
    client = OkxAccountClient()
    monkeypatch.setattr("app.execution.okx_account_client.get_settings", lambda: SimpleNamespace(okx_api_key="k", okx_api_secret="s", okx_api_passphrase=""))
    with pytest.raises(OkxCredentialsMissing):
        client._signed_headers("GET", "/api/v5/account/balance")


class _FakeResponse:
    def __init__(self, payload: dict, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def raise_for_status(self) -> None:
        pass

    async def json(self) -> dict:
        return self._payload

    async def text(self) -> str:
        import json as _json
        return _json.dumps(self._payload)

    async def __aenter__(self) -> "_FakeResponse":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


class _FakeSession:
    def __init__(self, payload: dict, captured: dict) -> None:
        self._payload = payload
        self._captured = captured

    def get(self, url: str, headers=None, params=None, timeout=None):
        self._captured["url"] = url
        self._captured["headers"] = headers
        return _FakeResponse(self._payload)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _fake_settings():
    return SimpleNamespace(okx_api_key="test-key", okx_api_secret="test-secret", okx_api_passphrase="test-pass")


async def test_get_account_snapshot_sends_signed_headers(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_account_client.get_settings", _fake_settings)
    client = OkxAccountClient()
    with patch("app.execution.okx_account_client.aiohttp.ClientSession", return_value=_FakeSession(BALANCE_FIXTURE, captured)):
        snap = await client.get_account_snapshot()
    assert snap.balance_usdt() == pytest.approx(34.99324042)
    assert "/api/v5/account/balance" in captured["url"]
    assert captured["headers"]["OK-ACCESS-KEY"] == "test-key"
    assert captured["headers"]["OK-ACCESS-PASSPHRASE"] == "test-pass"
    assert "OK-ACCESS-SIGN" in captured["headers"]
    assert "OK-ACCESS-TIMESTAMP" in captured["headers"]


async def test_get_trade_fee_includes_inst_id_and_inst_type(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr("app.execution.okx_account_client.get_settings", _fake_settings)
    client = OkxAccountClient()
    with patch("app.execution.okx_account_client.aiohttp.ClientSession", return_value=_FakeSession(FEE_FIXTURE, captured)):
        fee = await client.get_trade_fee("RVN/USDT")
    assert fee.taker_fee_rate == pytest.approx(0.001)
    assert "instType=SPOT" in captured["url"]
    assert "instId=RVN-USDT" in captured["url"]


async def test_check_connectivity_reports_credentials_missing_without_a_network_call(monkeypatch):
    monkeypatch.setattr("app.execution.okx_account_client.get_settings", lambda: SimpleNamespace(okx_api_key="", okx_api_secret="", okx_api_passphrase=""))
    client = OkxAccountClient()
    with patch("app.execution.okx_account_client.aiohttp.ClientSession") as mock_session_cls:
        result = await client.check_connectivity()
    mock_session_cls.assert_not_called()
    assert result.reachable is False
    assert result.credentials_configured is False
