from app.operations.dangerous_module_guard import find_loaded_dangerous_modules


def test_empty_when_nothing_dangerous_loaded():
    loaded = ["app.execution.binance_account_client", "app.execution.true_economic_ledger", "os", "sys"]
    assert find_loaded_dangerous_modules(loaded) == []


def test_detects_binance_live_trade_client():
    loaded = ["app.execution.binance_account_client", "app.execution.binance_live_trade_client"]
    assert find_loaded_dangerous_modules(loaded) == ["app.execution.binance_live_trade_client"]


def test_detects_bybit_live_trade_client():
    loaded = ["app.execution.bybit_live_trade_client"]
    assert find_loaded_dangerous_modules(loaded) == ["app.execution.bybit_live_trade_client"]


def test_detects_okx_live_trade_client():
    loaded = ["app.execution.okx_live_trade_client"]
    assert find_loaded_dangerous_modules(loaded) == ["app.execution.okx_live_trade_client"]


def test_detects_all_three_simultaneously():
    loaded = [
        "app.execution.binance_live_trade_client", "app.execution.bybit_live_trade_client",
        "app.execution.okx_live_trade_client", "app.execution.okx_account_client",
    ]
    result = find_loaded_dangerous_modules(loaded)
    assert set(result) == {
        "app.execution.binance_live_trade_client", "app.execution.bybit_live_trade_client", "app.execution.okx_live_trade_client",
    }


def test_read_only_okx_account_client_is_never_flagged():
    """A substring match on 'okx_live_trade_client' must not accidentally
    also flag the read-only okx_account_client -- they are deliberately
    different module names, not a prefix relationship."""
    assert find_loaded_dangerous_modules(["app.execution.okx_account_client"]) == []


def test_real_shadow_script_import_set_passes():
    """The exact set of app/ modules the real V5 three-exchange shadow
    script imports -- this must always pass, and is the closest thing to
    an end-to-end proof available for a script that lives outside the
    repo."""
    real_imports = [
        "app.config.settings", "app.database.session", "app.execution.binance_account_client",
        "app.execution.bybit_client", "app.execution.capital_rebalancer", "app.execution.okx_account_client",
        "app.execution.true_economic_ledger", "app.execution.true_economic_pretrade", "app.reporting.short_term_regime",
        "app.scanner.cross_exchange_scanner", "app.scanner.market_snapshot",
    ]
    assert find_loaded_dangerous_modules(real_imports) == []
