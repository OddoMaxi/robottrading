from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from app.reporting.short_term_regime import (
    CONFIRMATION_WINDOW_SECONDS,
    ShortTermRegime,
    classify_short_term_regime,
    compute_short_term_regime,
)


@dataclass
class _FakeRow:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    observed_at: datetime
    net_profit_usd: float
    net_profit_per_1000usdt: float
    executable: bool
    continuity_status: str
    persistence_seconds: float


BASE_TIME = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)


def _row(seconds_ago, net_profit=1.0, per_1000=5.0, status="continuation", persistence=60.0, executable=True):
    return _FakeRow(
        symbol="RVN/USDT", buy_exchange="binance", sell_exchange="bybit",
        observed_at=BASE_TIME - timedelta(seconds=seconds_ago),
        net_profit_usd=net_profit, net_profit_per_1000usdt=per_1000, executable=executable,
        continuity_status=status, persistence_seconds=persistence,
    )


# ---- classify_short_term_regime (pure) -----------------------------------


def test_no_edge_when_not_positive_now():
    regime, reason = classify_short_term_regime(edge_now_positive=False, confirmations_recent=10, current_streak_seconds=1000, min_confirmations=3)
    assert regime == ShortTermRegime.NO_EDGE


def test_flash_when_positive_but_too_few_confirmations():
    regime, reason = classify_short_term_regime(edge_now_positive=True, confirmations_recent=1, current_streak_seconds=10, min_confirmations=3)
    assert regime == ShortTermRegime.FLASH


def test_confirmed_short_term_reachable_without_waiting_minutes():
    """The exact point of item 4: CONFIRMED_SHORT_TERM must be reachable
    with a short streak, as long as there are enough independent recent
    confirmations."""
    regime, reason = classify_short_term_regime(edge_now_positive=True, confirmations_recent=3, current_streak_seconds=20, min_confirmations=3)
    assert regime == ShortTermRegime.CONFIRMED_SHORT_TERM


def test_persistent_when_streak_exceeds_five_minutes():
    regime, reason = classify_short_term_regime(edge_now_positive=True, confirmations_recent=3, current_streak_seconds=301, min_confirmations=3)
    assert regime == ShortTermRegime.PERSISTENT


def test_strong_persistent_when_streak_exceeds_fifteen_minutes():
    regime, reason = classify_short_term_regime(edge_now_positive=True, confirmations_recent=3, current_streak_seconds=901, min_confirmations=3)
    assert regime == ShortTermRegime.STRONG_PERSISTENT


def test_long_streak_qualifies_even_with_few_raw_confirmation_rows():
    """A long streak alone is sufficient — never require BOTH a long
    streak AND many confirmations."""
    regime, reason = classify_short_term_regime(edge_now_positive=True, confirmations_recent=1, current_streak_seconds=920, min_confirmations=3)
    assert regime == ShortTermRegime.STRONG_PERSISTENT


# ---- compute_short_term_regime (pure, over rows) --------------------------


def test_edge_now_reflects_the_latest_row_only():
    rows = [_row(600, net_profit=-1.0), _row(0, net_profit=2.0)]
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert summary.edge_now_positive is True


def test_current_streak_zero_when_latest_row_is_not_a_streak():
    rows = [_row(0, status="none", persistence=0.0, net_profit=-1.0, executable=False)]
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert summary.current_streak_seconds == 0.0
    assert summary.regime == ShortTermRegime.NO_EDGE


def test_confirmations_recent_excludes_rows_outside_the_confirmation_window():
    rows = [
        _row(CONFIRMATION_WINDOW_SECONDS + 60, net_profit=1.0),  # too old — must not count
        _row(200, net_profit=1.0),
        _row(100, net_profit=1.0),
        _row(0, net_profit=1.0),
    ]
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert summary.confirmations_recent == 3  # only the 3 within the window


def test_confirmations_recent_excludes_non_positive_rows():
    rows = [_row(200, net_profit=-1.0), _row(100, net_profit=1.0), _row(0, net_profit=1.0)]
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert summary.confirmations_recent == 2


def test_confirmations_recent_excludes_continuity_status_none():
    """A row where the streak had already broken (continuity_status
    "none") must not count as an independent positive confirmation even
    if net_profit_usd happens to still look positive in that row."""
    rows = [
        _row(100, net_profit=1.0, status="none"),
        _row(50, net_profit=1.0, status="new"),
        _row(0, net_profit=1.0, status="continuation"),
    ]
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert summary.confirmations_recent == 2


def test_windows_computed_for_every_stated_horizon():
    rows = [_row(s) for s in (10, 50, 110, 170, 250, 800)]
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert set(summary.windows.keys()) == {"30sec", "1min", "2min", "3min", "5min", "15min"}
    assert summary.windows["30sec"].observations == 1
    assert summary.windows["15min"].observations == 6  # all rows fall within 15 minutes


def test_rows_out_of_order_are_still_handled_correctly():
    rows = [_row(0, net_profit=2.0), _row(600, net_profit=-1.0)]  # latest row listed first
    summary = compute_short_term_regime("RVN/USDT", "binance", "bybit", rows, min_confirmations=3)
    assert summary.edge_now_positive is True  # must anchor on the truly-latest observed_at, not list order
