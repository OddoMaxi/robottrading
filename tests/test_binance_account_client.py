from app.execution.binance_account_client import _parse_account_snapshot, _parse_api_restrictions, _parse_trade_fee

TRADE_FEE_FIXTURE = [{"symbol": "BTCUSDT", "makerCommission": "0.001000", "takerCommission": "0.001000"}]

ACCOUNT_FIXTURE = {
    "canTrade": True,
    "canWithdraw": True,  # account-level KYC flag — deliberately NOT the same thing api_restrictions checks
    "canDeposit": True,
    "accountType": "SPOT",
    "permissions": ["SPOT"],
    "balances": [
        {"asset": "USDT", "free": "13.20", "locked": "0.00"},
        {"asset": "BTC", "free": "0.00000000", "locked": "0.00000000"},
        {"asset": "BNB", "free": "0.01000000", "locked": "0.00000000"},
    ],
}

RESTRICTIONS_FIXTURE_READ_ONLY = {
    "ipRestrict": True,
    "enableReading": True,
    "enableWithdrawals": False,
    "enableInternalTransfer": False,
    "enableMargin": False,
    "enableFutures": False,
    "enableSpotAndMarginTrading": False,
}

RESTRICTIONS_FIXTURE_WITHDRAWAL_ENABLED = {**RESTRICTIONS_FIXTURE_READ_ONLY, "enableWithdrawals": True}


def test_parse_account_snapshot_only_keeps_nonzero_balances():
    snapshot = _parse_account_snapshot(ACCOUNT_FIXTURE, now=0.0)
    assets = {b.asset for b in snapshot.balances}
    assert assets == {"USDT", "BNB"}  # BTC (0/0) is dropped
    assert snapshot.can_withdraw is True  # account-level flag, parsed as-is
    assert snapshot.balance_usdt() == 13.20


def test_parse_account_snapshot_balance_usdt_defaults_to_zero_when_absent():
    snapshot = _parse_account_snapshot({"balances": []}, now=0.0)
    assert snapshot.balance_usdt() == 0.0


def test_balance_of_any_asset():
    snapshot = _parse_account_snapshot(ACCOUNT_FIXTURE, now=0.0)
    assert snapshot.balance_of("BNB") == 0.01
    assert snapshot.balance_of("DOGE") == 0.0


def test_parse_api_restrictions_read_only_key():
    restrictions = _parse_api_restrictions(RESTRICTIONS_FIXTURE_READ_ONLY, now=0.0)
    assert restrictions.enable_reading is True
    assert restrictions.enable_withdrawals is False
    assert restrictions.enable_spot_and_margin_trading is False
    assert restrictions.ip_restrict is True


def test_parse_api_restrictions_flags_withdrawal_enabled_key():
    """This is the field item 2's safety requirement must actually be
    checked against — not BinanceAccountSnapshot.can_withdraw."""
    restrictions = _parse_api_restrictions(RESTRICTIONS_FIXTURE_WITHDRAWAL_ENABLED, now=0.0)
    assert restrictions.enable_withdrawals is True


def test_parse_trade_fee_finds_matching_symbol():
    fee = _parse_trade_fee(TRADE_FEE_FIXTURE, "BTCUSDT", now=0.0)
    assert fee is not None
    assert fee.maker_fee_rate == 0.001
    assert fee.taker_fee_rate == 0.001


def test_parse_trade_fee_returns_none_for_missing_symbol():
    """Never invents a fee for a symbol Binance didn't return — the
    caller must fall back to the ESTIMATED default, not a fabricated real one."""
    assert _parse_trade_fee(TRADE_FEE_FIXTURE, "ETHUSDT", now=0.0) is None
