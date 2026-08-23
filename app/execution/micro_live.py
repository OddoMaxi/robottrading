"""MICRO-LIVE dry-run orchestrator (Phase 2D, items 3+5+9, user directive,
2026-08-23) — READ-ONLY. No order is placed anywhere in this module.

Wires together the pieces built for Phase 2D into one call,
`observe_reality_quote(opp)`, invoked from main.py's CEX detection loop
for every cross_exchange opportunity that has a Binance leg (this phase
is Binance-only by the user's own directive — an opportunity whose two
legs are both non-Binance exchanges has no Binance order to dry-run).

Every failure mode (no credentials, network error, symbol not found,
rate limit) is caught and recorded, never raised into the detection
loop — this observation layer must be exactly as safe to fail as the
Phase 2B CEX scan telemetry tap it's modeled on: a broken Binance
connection degrades observability, never paper execution.

Account/exchange-info are cached with a TTL so the hot detection loop
never makes a SIGNED call per opportunity (rate-limit and credential-
exposure discipline) — only public book-ticker/depth calls happen per
opportunity, which is what varies opportunity to opportunity anyway.
"""

import logging
import time
from dataclasses import dataclass

from app.execution.binance_account_client import (
    BinanceAccountClient,
    BinanceAccountSnapshot,
    BinanceCredentialsMissing,
)
from app.execution.binance_filters import SymbolNotFound, SymbolRules, parse_symbol_rules
from app.execution.reality_quote import DEFAULT_TAKER_FEE_RATE, RealityQuote, compute_reality_quote
from app.opportunity.models import Opportunity

logger = logging.getLogger(__name__)

ACCOUNT_SNAPSHOT_TTL_SECONDS = 60.0
EXCHANGE_INFO_TTL_SECONDS = 3600.0  # symbol filters change rarely


def _binance_symbol(opp_symbol: str) -> str:
    return opp_symbol.replace("/", "")


def _binance_leg(opp: Opportunity) -> dict | None:
    for leg in opp.legs:
        if leg.get("exchange") == "binance":
            return leg
    return None


@dataclass(slots=True)
class MicroLiveObservation:
    quote: RealityQuote
    strategy: str
    observed_at: float


class MicroLiveState:
    """In-memory session state — same convention as
    app.orchestration.global_allocator's own singleton. Never persisted;
    the dashboard/final report reads it directly via the engine API,
    exactly like Master Status (Phase 2C)."""

    def __init__(self) -> None:
        self.observations: list[MicroLiveObservation] = []
        self.account_snapshot: BinanceAccountSnapshot | None = None
        self.account_snapshot_error: str | None = None
        self.last_connectivity_reachable: bool | None = None
        self.last_connectivity_detail: str | None = None

    def record(self, quote: RealityQuote, strategy: str, now: float | None = None) -> None:
        self.observations.append(MicroLiveObservation(quote=quote, strategy=strategy, observed_at=now or time.time()))

    def summary(self) -> dict:
        total = len(self.observations)
        executable = [o for o in self.observations if o.quote.executable]
        non_executable = [o for o in self.observations if not o.quote.executable]
        rejection_reasons: dict[str, int] = {}
        for obs in non_executable:
            bucket = _rejection_bucket(obs.quote)
            rejection_reasons[bucket] = rejection_reasons.get(bucket, 0) + 1
        return {
            "opportunities_observed": total,
            "executable_with_cap": len(executable),
            "non_executable": len(non_executable),
            "rejection_reasons": rejection_reasons,
            "avg_estimated_fees_usd": _avg([o.quote.estimated_fees_usd for o in self.observations]),
            "avg_estimated_slippage_pct": _avg([o.quote.estimated_slippage_pct for o in self.observations]),
            "avg_net_profit_after_real_constraints_usd": _avg(
                [o.quote.estimated_net_profit_after_real_constraints_usd for o in self.observations]
            ),
        }


def _rejection_bucket(quote: RealityQuote) -> str:
    if not quote.min_notional_pass:
        return "min_notional"
    if not quote.lot_size_pass:
        return "lot_size"
    if not quote.balance_pass:
        return "balance"
    if quote.estimated_net_profit_after_real_constraints_usd <= 0:
        return "net_profit_leq_zero"
    return "other"


def _avg(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


micro_live_state = MicroLiveState()


class MicroLiveOrchestrator:
    def __init__(self, client: BinanceAccountClient | None = None) -> None:
        self._client = client or BinanceAccountClient()
        self._account_snapshot: BinanceAccountSnapshot | None = None
        self._account_snapshot_fetched_at = 0.0
        self._exchange_info_cache: dict[str, tuple[float, SymbolRules]] = {}

    async def check_connectivity(self):
        return await self._client.check_connectivity()

    async def get_account_snapshot(self, now: float | None = None) -> BinanceAccountSnapshot | None:
        """Public entry point for callers (the /micro-live/status endpoint)
        that want the real balance without waiting for a cross_exchange
        opportunity to trigger it — still TTL-cached, still never a
        signed call more often than ACCOUNT_SNAPSHOT_TTL_SECONDS."""
        return await self._get_account_snapshot(now if now is not None else time.time())

    async def _get_account_snapshot(self, now: float) -> BinanceAccountSnapshot | None:
        if self._account_snapshot is not None and now - self._account_snapshot_fetched_at < ACCOUNT_SNAPSHOT_TTL_SECONDS:
            return self._account_snapshot
        try:
            snapshot = await self._client.get_account_snapshot()
        except BinanceCredentialsMissing as exc:
            micro_live_state.account_snapshot_error = str(exc)
            return None
        except Exception as exc:
            micro_live_state.account_snapshot_error = f"account fetch failed: {exc}"
            return None
        self._account_snapshot = snapshot
        self._account_snapshot_fetched_at = now
        micro_live_state.account_snapshot = snapshot
        micro_live_state.account_snapshot_error = None
        return snapshot

    async def _get_symbol_rules(self, symbol: str, now: float) -> SymbolRules | None:
        cached = self._exchange_info_cache.get(symbol)
        if cached is not None and now - cached[0] < EXCHANGE_INFO_TTL_SECONDS:
            return cached[1]
        try:
            info = await self._client.get_exchange_info(symbols=[symbol])
            rules = parse_symbol_rules(info, symbol)
        except SymbolNotFound:
            return None
        except Exception as exc:
            logger.warning("micro-live: exchangeInfo fetch failed for %s: %s", symbol, exc)
            return None
        self._exchange_info_cache[symbol] = (now, rules)
        return rules

    async def observe_reality_quote(self, opp: Opportunity, now: float | None = None) -> RealityQuote | None:
        """Best-effort, never raises. Returns None (and records nothing)
        if any prerequisite (credentials, symbol data, live quote) isn't
        available — an absence here just means one fewer observation, not
        a paper-execution-affecting error."""
        now = now if now is not None else time.time()
        leg = _binance_leg(opp)
        if leg is None:
            return None  # this phase is Binance-only — no Binance leg, nothing to dry-run

        symbol = _binance_symbol(opp.symbol)
        side = str(leg.get("side", "BUY")).upper()

        try:
            from app.config.settings import get_settings

            settings = get_settings()

            snapshot = await self._get_account_snapshot(now)
            rules = await self._get_symbol_rules(symbol, now)
            if rules is None:
                return None
            balance_usdt = snapshot.balance_usdt() if snapshot is not None else 0.0

            book_ticker = await self._client.get_book_ticker(symbol)
            best_bid = float(book_ticker["bidPrice"])
            best_ask = float(book_ticker["askPrice"])

            depth = await self._client.get_order_book_depth(symbol, limit=20)
            side_levels_key = "asks" if side == "BUY" else "bids"
            depth_levels = [(float(p), float(q)) for p, q in depth.get(side_levels_key, [])]

            quote = compute_reality_quote(
                opportunity_id=opp.id,
                symbol=symbol,
                side=side,
                master_requested_size_usd=opp.capital_usd or 0.0,
                gross_spread_pct=opp.gross_spread_pct,
                rules=rules,
                best_bid=best_bid,
                best_ask=best_ask,
                depth_levels=depth_levels,
                account_balance_usdt=balance_usdt,
                micro_live_cap_usdt=settings.micro_live_cap_usdt,
                taker_fee_rate=DEFAULT_TAKER_FEE_RATE,
                fee_source="estimated_default",
                now=now,
            )
        except Exception as exc:
            logger.warning("micro-live: reality quote failed for %s: %s", opp.symbol, exc)
            return None

        micro_live_state.record(quote, strategy=str(opp.strategy), now=now)
        return quote


micro_live_orchestrator = MicroLiveOrchestrator()
