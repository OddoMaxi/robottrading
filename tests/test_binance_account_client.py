from app.execution.binance_account_client import _parse_account_snapshot, _parse_api_restrictions

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
