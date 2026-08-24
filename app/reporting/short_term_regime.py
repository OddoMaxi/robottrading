"""SHORT-TERM REGIME DETECTOR (user directive, 2026-08-24, "FINAL
SIMPLIFICATION") — this strategy is short-term cross-exchange arbitrage:
an opportunity can be excellent for seconds or a few minutes, and a 1h
or 24h positive AVERAGE is not a necessary condition for it to be worth
acting on right now. This module measures the CURRENT regime (edge now,
and over 30s/1min/2min/3min/5min/15min windows) from the same persisted,
market_scope="live" observations app.reporting.altcoin_scan_report
already aggregates — reusing its row-fetching and grouping helpers
rather than re-querying independently.

"Ne confonds pas 50 lectures du même quote figé avec 50 confirmations
indépendantes" — every persisted row already comes from a genuinely
fresh, real order-book fetch (app.scanner.market_snapshot never caches
book/depth data), so counting distinct persisted rows within a window IS
counting independent confirmations, not repeats of one stale read.

Pure aggregation over already-persisted rows; never fetches market data
itself, never influences what gets persisted, and cannot place an order.
"""

import statistics
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AltcoinScanObservationRecord
from app.reporting.altcoin_scan_report import _fetch_observations, _group_by_symbol_and_direction

# Item 3's own list — informational windows shown in every report; the
# classification itself only needs a couple of these (see
# classify_short_term_regime) but all are computed for transparency.
SHORT_TERM_WINDOW_SECONDS: dict[str, int] = {
    "30sec": 30,
    "1min": 60,
    "2min": 120,
    "3min": 180,
    "5min": 300,
    "15min": 900,
}

# Round, stated thresholds (never fitted to any observed data) — the
# SAME min_expected_reuse_count already used project-wide for "never
# pre-position for a one-off spread", now evaluated over a short recent
# window instead of 24h (item 2: keep the concept, fix the time horizon).
CONFIRMATION_WINDOW_SECONDS = 300  # 5 min
PERSISTENT_THRESHOLD_SECONDS = 300  # 5 min
STRONG_PERSISTENT_THRESHOLD_SECONDS = 900  # 15 min
DEFAULT_LOOKBACK_MINUTES = 15.0


class ShortTermRegime(StrEnum):
    NO_EDGE = "NO_EDGE"
    FLASH = "FLASH"
    CONFIRMED_SHORT_TERM = "CONFIRMED_SHORT_TERM"
    PERSISTENT = "PERSISTENT"
    STRONG_PERSISTENT = "STRONG_PERSISTENT"


@dataclass(slots=True)
class ShortTermWindowStats:
    window_label: str
    observations: int
    positive_count: int
    net_profit_per_1000usdt_mean: float


@dataclass(slots=True)
class ShortTermRegimeSummary:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    edge_now_positive: bool
    edge_now_net_profit_per_1000usdt: float | None
    current_streak_seconds: float  # how long the CURRENT positive streak has run, from the scanner's own real-time ContinuityTracker data
    windows: dict[str, ShortTermWindowStats] = field(default_factory=dict)
    confirmations_recent: int = 0  # independent positive observations within CONFIRMATION_WINDOW_SECONDS
    regime: ShortTermRegime = ShortTermRegime.NO_EDGE
    regime_reason: str = ""
    # Informational only (item 2/6) — never a gate. Filled in by the
    # caller, which already has wider-window data from
    # app.reporting.altcoin_scan_report.build_altcoin_scan_report.
    mean_net_profit_1h_usdt: float | None = None
    mean_net_profit_24h_usdt: float | None = None


def classify_short_term_regime(
    edge_now_positive: bool, confirmations_recent: int, current_streak_seconds: float, min_confirmations: int
) -> tuple[ShortTermRegime, str]:
    """Pure function. CONFIRMED_SHORT_TERM is reachable WITHOUT waiting
    for multi-minute persistence — item 4: 'CONFIRMED_SHORT_TERM doit
    déjà pouvoir devenir tradable.' PERSISTENT/STRONG_PERSISTENT are a
    strictly higher bar (checked first, since a long-held streak also
    trivially clears the confirmation-count bar) that increases
    confidence but is never required for the first small trade."""
    if not edge_now_positive:
        return ShortTermRegime.NO_EDGE, "edge is not positive right now"
    if current_streak_seconds >= STRONG_PERSISTENT_THRESHOLD_SECONDS:
        return ShortTermRegime.STRONG_PERSISTENT, f"edge has held positive for {current_streak_seconds:.0f}s (>= 15min) — durable"
    if current_streak_seconds >= PERSISTENT_THRESHOLD_SECONDS:
        return ShortTermRegime.PERSISTENT, f"edge has held positive for {current_streak_seconds:.0f}s (>= 5min)"
    if confirmations_recent >= min_confirmations:
        return (
            ShortTermRegime.CONFIRMED_SHORT_TERM,
            f"{confirmations_recent} independent positive confirmations in the last {CONFIRMATION_WINDOW_SECONDS}s, edge positive now",
        )
    return (
        ShortTermRegime.FLASH,
        f"edge positive now but only {confirmations_recent} confirmation(s) in the last {CONFIRMATION_WINDOW_SECONDS}s — need >= {min_confirmations}",
    )


def compute_short_term_regime(
    symbol: str, buy_exchange: str, sell_exchange: str, rows: list[AltcoinScanObservationRecord], min_confirmations: int
) -> ShortTermRegimeSummary:
    """Pure function over already-fetched rows for ONE (symbol,
    buy_exchange, sell_exchange). Anchored to the LATEST row's own
    observed_at, never wall-clock time — the most recent persisted
    observation may itself be up to one scan interval old."""
    rows_sorted = sorted(rows, key=lambda r: r.observed_at)
    latest = rows_sorted[-1]
    now_ts = latest.observed_at
    edge_now_positive = bool(latest.executable) and float(latest.net_profit_usd) > 0
    current_streak_seconds = float(latest.persistence_seconds) if latest.continuity_status in ("new", "continuation") else 0.0

    def _window_stats(label: str, seconds: int) -> ShortTermWindowStats:
        cutoff = now_ts - timedelta(seconds=seconds)
        window_rows = [r for r in rows_sorted if r.observed_at >= cutoff]
        positive = [r for r in window_rows if r.executable and float(r.net_profit_usd) > 0]
        per_1000 = [float(r.net_profit_per_1000usdt) for r in window_rows]
        return ShortTermWindowStats(
            window_label=label,
            observations=len(window_rows),
            positive_count=len(positive),
            net_profit_per_1000usdt_mean=statistics.fmean(per_1000) if per_1000 else 0.0,
        )

    windows = {label: _window_stats(label, seconds) for label, seconds in SHORT_TERM_WINDOW_SECONDS.items()}

    confirmation_cutoff = now_ts - timedelta(seconds=CONFIRMATION_WINDOW_SECONDS)
    confirmations_recent = sum(
        1
        for r in rows_sorted
        if r.observed_at >= confirmation_cutoff and r.continuity_status in ("new", "continuation") and r.executable and float(r.net_profit_usd) > 0
    )

    regime, reason = classify_short_term_regime(edge_now_positive, confirmations_recent, current_streak_seconds, min_confirmations)

    return ShortTermRegimeSummary(
        symbol=symbol,
        buy_exchange=buy_exchange,
        sell_exchange=sell_exchange,
        edge_now_positive=edge_now_positive,
        edge_now_net_profit_per_1000usdt=float(latest.net_profit_per_1000usdt),
        current_streak_seconds=current_streak_seconds,
        windows=windows,
        confirmations_recent=confirmations_recent,
        regime=regime,
        regime_reason=reason,
    )


async def build_short_term_regimes(
    session: AsyncSession, min_confirmations: int, lookback_minutes: float = DEFAULT_LOOKBACK_MINUTES
) -> dict[tuple[str, str, str], ShortTermRegimeSummary]:
    """One query (reusing altcoin_scan_report's own row fetcher, already
    filtered to market_scope="live" by default) plus pure in-memory
    grouping — no per-symbol round trips."""
    since = (datetime.now(UTC) - timedelta(minutes=lookback_minutes)).replace(tzinfo=None)
    rows = await _fetch_observations(session, since, None)
    groups = _group_by_symbol_and_direction(rows)
    return {key: compute_short_term_regime(key[0], key[1], key[2], group_rows, min_confirmations) for key, group_rows in groups.items()}
