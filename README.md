# Multi-Market Crypto Arbitrage Engine — V1

Market observation + paper trading only. No real capital. See the cahier des
charges for the full spec; this follows the architecture from section 26.

## Status

End to end and running: WebSocket collectors for Binance/OKX/Bybit spot
top-of-book, REST funding-rate pollers for their perpetuals, all four engines
(Stablecoin, Cross-Exchange, Triangular, Funding), the Fee/Liquidity/Slippage
analytics, Opportunity scoring + classification, Paper Trading against 5
virtual portfolios, the 13-table Postgres schema, the FastAPI API, and the
Streamlit dashboard. `main.py` runs the collectors, funding pollers, and a
detection/paper-trading loop as background asyncio tasks alongside the API.

22 tests pass (`pytest`), covering symbol translation, per-exchange payload
parsing, engine math (cross-exchange, triangular), and the full
detect → score → classify → paper-trade pipeline against synthetic quotes.
The 13 SQLAlchemy models were verified to compile to valid PostgreSQL DDL.

**Known limitations, by design for V1:**
- Collectors use top-of-book only (`bookTicker`/`tickers` streams), not full
  order-book depth — the Liquidity/Slippage engines treat that single level
  as the whole book. A real depth feed is the natural next step once this
  is validated against live data.
- The Opportunity Score's duration/volatility/latency factors are held at a
  neutral placeholder (0.5) until the Duration Engine accumulates history —
  net profit and fill ratio are the only factors driving the score for now.
- OKX's funding poller uses mark price as an index-price stand-in (a third
  REST call would get the real index price).
- **Live WebSocket/REST connectivity to Binance/OKX/Bybit was not verified
  in this environment — this sandbox has no outbound network access.** The
  parsing logic matches each exchange's documented public payload shape, but
  should be watched closely on first real run (see below).

## Quickstart

```bash
cp .env.example .env
python3.12 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

docker compose up -d          # Postgres (TimescaleDB) + Redis

python main.py                 # API on :8000, runs collectors + detection loop, creates tables on boot
streamlit run dashboard/app.py # Dashboard on :8501

pytest
```

On first real run, watch the logs for each collector's "connected" message
and for repeated reconnect/backoff messages, which would mean a payload
shape assumption is wrong (most likely candidate: Bybit's delta-only ticker
updates, or an OKX ping/pong timing issue).

## Next steps

1. Run it live for a stretch and fix whatever the real payloads reveal.
2. Add full order-book depth collection so the Liquidity Engine can test all
   8 capital tiers (currently only sees top-of-book).
3. Add the Duration Engine (peak/closed timestamps, persistence tracking)
   so the Opportunity Score's duration/volatility factors stop being
   placeholders.
4. Build out the richer dashboard views (sections 28-32): live opportunities
   table with filters, per-strategy comparison, funding dashboard, daily
   report generation.
