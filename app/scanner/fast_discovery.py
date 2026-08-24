"""STAGE A — FAST DISCOVERY (Inventory Manager V2, user directive,
2026-08-24) — cheaply pre-filters the FULL dynamic Binance∩Bybit
universe (~240 symbols) down to a small candidate list before STAGE B's
expensive, real per-symbol validation (app.scanner.cross_exchange_scanner.
scan_symbol — real fees, real depth, real slippage) ever runs.

The whole point: two bulk-ticker HTTP calls (one per exchange, covering
EVERY symbol at once) replace what would otherwise be hundreds of
per-symbol requests. Binance's /api/v3/ticker/24hr and Bybit's
/v5/market/tickers?category=spot both already carry bid/ask AND 24h
volume/price-change in the same payload, so STAGE A gets a raw
cross-exchange spread signal and a momentum signal from the same two
calls — no extra requests for the latter.

Pure computation only (discover_candidates takes already-fetched
ticker dicts, does no I/O itself) — this module places no order and
holds no execution authority, same isolation guarantee as the rest of
app/scanner (see tests/test_scanner_isolation.py).

Item 7 of the directive ("ne confonds pas momentum et arbitrage") is
enforced structurally here: a candidate can only be promoted for one of
two reasons — "raw_spread" (it cleared the minimum gross cross-exchange
spread floor) or "momentum" (unusual 24h volume/price-change, used ONLY
to top up a few EXTRA exploratory slots after every raw_spread candidate
is already included, never to bump a raw_spread candidate out). STAGE B
still independently decides real executability from real fees/depth;
STAGE A's momentum tag never reaches the Inventory Score.
"""

from dataclasses import dataclass, field


@dataclass(slots=True)
class BulkTicker:
    symbol: str  # exchange-native, no slash (e.g. "ZROUSDT")
    bid: float
    ask: float
    quote_volume_24h: float
    price_change_pct_24h: float


def parse_binance_bulk_tickers(raw: list[dict]) -> dict[str, BulkTicker]:
    tickers: dict[str, BulkTicker] = {}
    for entry in raw:
        try:
            symbol = entry["symbol"]
            bid = float(entry["bidPrice"])
            ask = float(entry["askPrice"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        tickers[symbol] = BulkTicker(
            symbol=symbol,
            bid=bid,
            ask=ask,
            quote_volume_24h=float(entry.get("quoteVolume") or 0.0),
            price_change_pct_24h=float(entry.get("priceChangePercent") or 0.0),
        )
    return tickers


def parse_bybit_bulk_tickers(raw: dict) -> dict[str, BulkTicker]:
    tickers: dict[str, BulkTicker] = {}
    for entry in raw.get("result", {}).get("list", []):
        try:
            symbol = entry["symbol"]
            bid = float(entry["bid1Price"])
            ask = float(entry["ask1Price"])
        except (KeyError, TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0:
            continue
        # Bybit's price24hPcnt is a fraction (0.05 = +5%), Binance's
        # priceChangePercent is already in percent — normalized to
        # percent here so momentum_pct is comparable across exchanges.
        change_frac = entry.get("price24hPcnt")
        tickers[symbol] = BulkTicker(
            symbol=symbol,
            bid=bid,
            ask=ask,
            quote_volume_24h=float(entry.get("turnover24h") or 0.0),
            price_change_pct_24h=float(change_frac) * 100 if change_frac not in (None, "") else 0.0,
        )
    return tickers


@dataclass(slots=True)
class FastDiscoveryCandidate:
    symbol: str
    buy_exchange: str
    sell_exchange: str
    raw_gross_spread_pct: float
    buy_price: float
    sell_price: float
    momentum_pct: float
    promoted_reason: str | None = None  # "raw_spread" | "momentum" | None (not promoted)


@dataclass(slots=True)
class FastDiscoveryResult:
    universe_size: int
    fast_scanned_count: int  # == universe_size: STAGE A always evaluates every symbol present in both bulk snapshots
    raw_edge_count: int  # candidates that cleared min_raw_spread_pct, before any cap or momentum top-up
    candidates: list[FastDiscoveryCandidate] = field(default_factory=list)  # capped, STAGE B promotion list


def discover_candidates(
    universe: list[str],
    binance_tickers: dict[str, BulkTicker],
    bybit_tickers: dict[str, BulkTicker],
    min_raw_spread_pct: float,
    max_candidates: int,
    momentum_top_up: int = 0,
) -> FastDiscoveryResult:
    all_directions: list[FastDiscoveryCandidate] = []
    for symbol in universe:
        raw_symbol = symbol.replace("/", "")
        b = binance_tickers.get(raw_symbol)
        y = bybit_tickers.get(raw_symbol)
        if b is None or y is None:
            continue
        momentum_pct = max(abs(b.price_change_pct_24h), abs(y.price_change_pct_24h))

        binance_buy_spread = (y.bid - b.ask) / b.ask * 100
        all_directions.append(
            FastDiscoveryCandidate(
                symbol=symbol, buy_exchange="binance", sell_exchange="bybit",
                raw_gross_spread_pct=binance_buy_spread, buy_price=b.ask, sell_price=y.bid,
                momentum_pct=momentum_pct,
                promoted_reason="raw_spread" if binance_buy_spread >= min_raw_spread_pct else None,
            )
        )

        bybit_buy_spread = (b.bid - y.ask) / y.ask * 100
        all_directions.append(
            FastDiscoveryCandidate(
                symbol=symbol, buy_exchange="bybit", sell_exchange="binance",
                raw_gross_spread_pct=bybit_buy_spread, buy_price=y.ask, sell_price=b.bid,
                momentum_pct=momentum_pct,
                promoted_reason="raw_spread" if bybit_buy_spread >= min_raw_spread_pct else None,
            )
        )

    qualifying = [c for c in all_directions if c.promoted_reason == "raw_spread"]
    raw_edge_count = len(qualifying)
    qualifying.sort(key=lambda c: c.raw_gross_spread_pct, reverse=True)

    remaining_slots = max(0, max_candidates - len(qualifying))
    if remaining_slots > 0 and momentum_top_up > 0:
        not_yet_promoted = [c for c in all_directions if c.promoted_reason is None]
        not_yet_promoted.sort(key=lambda c: c.momentum_pct, reverse=True)
        for c in not_yet_promoted[: min(remaining_slots, momentum_top_up)]:
            c.promoted_reason = "momentum"
            qualifying.append(c)

    return FastDiscoveryResult(
        universe_size=len(universe),
        fast_scanned_count=len(universe),
        raw_edge_count=raw_edge_count,
        candidates=qualifying[:max_candidates],
    )
