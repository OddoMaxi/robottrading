import uuid

from app.execution.binance_filters import parse_symbol_rules
from app.execution.reality_quote import compute_reality_quote

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

RULES = parse_symbol_rules(EXCHANGE_INFO_FIXTURE, "BTCUSDT")

DEEP_BOOK = [(50_010.0, 1.0), (50_020.0, 1.0), (50_030.0, 1.0)]  # far more depth than 10 USDT could ever need


def test_reality_quote_executable_with_healthy_spread_and_deep_book():
    quote = compute_reality_quote(
        opportunity_id=uuid.uuid4(),
        symbol="BTCUSDT",
        side="BUY",
        master_requested_size_usd=500.0,  # what MASTER/paper wanted — larger than the micro-live cap
        gross_spread_pct=1.0,  # 1% edge, comfortably covers real fees/slippage on a deep book
        rules=RULES,
        best_bid=50_000.0,
        best_ask=50_010.0,
        depth_levels=DEEP_BOOK,
        account_balance_usdt=100.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.master_requested_size_usd == 500.0
    assert quote.exchange_valid_size_usd <= 10.0 + 1e-6  # never exceeds the micro-live cap even though MASTER wanted more
    assert quote.executable is True
    assert quote.reason is None
    assert quote.estimated_net_profit_after_real_constraints_usd > 0


def test_reality_quote_never_sizes_above_micro_live_cap_even_when_balance_is_huge():
    """item 6: PAPER_CAPITAL must never leak in — a large real balance
    must not let sizing exceed micro_live_cap_usdt."""
    quote = compute_reality_quote(
        opportunity_id=uuid.uuid4(),
        symbol="BTCUSDT",
        side="BUY",
        master_requested_size_usd=10_000.0,
        gross_spread_pct=1.0,
        rules=RULES,
        best_bid=50_000.0,
        best_ask=50_010.0,
        depth_levels=DEEP_BOOK,
        account_balance_usdt=1_000_000.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.exchange_valid_size_usd <= 10.0 + 1e-6


def test_reality_quote_rejects_when_spread_too_thin_to_cover_real_fees_and_slippage():
    quote = compute_reality_quote(
        opportunity_id=uuid.uuid4(),
        symbol="BTCUSDT",
        side="BUY",
        master_requested_size_usd=10.0,
        gross_spread_pct=0.01,  # far too thin to survive real 0.1% fee + any slippage
        rules=RULES,
        best_bid=50_000.0,
        best_ask=50_010.0,
        depth_levels=DEEP_BOOK,
        account_balance_usdt=100.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.executable is False
    assert "net profit" in quote.reason.lower()


def test_reality_quote_rejects_below_min_notional_at_micro_live_scale():
    """Directly measures whether 10 USDT survives a real MIN_NOTIONAL —
    the whole empirical point of item 9."""
    quote = compute_reality_quote(
        opportunity_id=uuid.uuid4(),
        symbol="BTCUSDT",
        side="BUY",
        master_requested_size_usd=10.0,
        gross_spread_pct=1.0,
        rules=RULES,
        best_bid=50_000.0,
        best_ask=50_010.0,
        depth_levels=DEEP_BOOK,
        account_balance_usdt=100.0,
        micro_live_cap_usdt=3.0,  # below the fixture's 5.00 USDT MIN_NOTIONAL
    )
    assert quote.executable is False
    assert quote.min_notional_pass is False


def test_reality_quote_flags_insufficient_book_depth_as_high_slippage():
    thin_book = [(50_010.0, 0.00001)]  # nowhere near enough to fill a 10 USDT order
    quote = compute_reality_quote(
        opportunity_id=uuid.uuid4(),
        symbol="BTCUSDT",
        side="BUY",
        master_requested_size_usd=10.0,
        gross_spread_pct=1.0,
        rules=RULES,
        best_bid=50_000.0,
        best_ask=50_010.0,
        depth_levels=thin_book,
        account_balance_usdt=100.0,
        micro_live_cap_usdt=10.0,
    )
    assert quote.estimated_slippage_pct >= 100.0
    assert quote.executable is False


def test_reality_quote_never_reads_paper_capital_module():
    """Structural check: this module must not import the $10,000 PAPER
    allocator at all — sizing must be derivable purely from its own
    arguments (item 6)."""
    import ast
    from pathlib import Path

    source = Path("app/execution/reality_quote.py").read_text()
    tree = ast.parse(source)
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "app.orchestration.global_allocator" not in imported
