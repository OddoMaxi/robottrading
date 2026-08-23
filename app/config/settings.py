from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # App
    app_name: str = "multi-arbitrage-engine"
    environment: str = "development"
    log_level: str = "INFO"

    # Database
    database_url: str = "postgresql+asyncpg://arbitrage:arbitrage@localhost:5432/arbitrage"

    # Cache
    redis_url: str = "redis://localhost:6379/0"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8000

    # Dashboard-only — where to reach the engine's own FastAPI app for
    # endpoints backed by live in-process state (kill switch, live capital
    # pool, Micro Live Readiness) that the dashboard's DB-only reporting
    # layer has no other way to read. Same host in every deployed
    # environment so far (engine and dashboard are two systemd services on
    # one VPS) — overridable via env if that ever changes.
    engine_api_base_url: str = "http://localhost:8000"

    # Exchanges enabled in V1 (priority order per cahier des charges section 3)
    enabled_exchanges: list[str] = ["binance", "okx", "bybit"]

    # Exchange API credentials, read exclusively from the environment/.env
    # — never hardcoded, never logged, never written to the DB, never shown
    # on the dashboard (Phase 2D, item 2). As of Phase 2D, binance_api_key/
    # secret are used for REAL read-only mainnet calls (account balances,
    # exchange filters) by app.execution.binance_account_client — the key
    # must be provisioned with reading permissions only; withdrawal must be
    # disabled on the Binance side, this app has no way to enforce that
    # remotely, only to never call an endpoint that would use it.
    binance_api_key: str = ""
    binance_api_secret: str = ""
    okx_api_key: str = ""
    okx_api_secret: str = ""
    okx_api_passphrase: str = ""
    bybit_api_key: str = ""
    bybit_api_secret: str = ""

    # Reality Engine spec, section 57 — Binance Spot Testnet preparation.
    # Deliberately separate fields from binance_api_key/secret above (those
    # are reserved for a real live connection later): mixing testnet and
    # live credentials in one field is exactly the kind of accident that
    # shouldn't be possible. Unused for any authenticated call in V1 — see
    # app.execution.binance_testnet_client's own docstring.
    binance_testnet_api_key: str = ""
    binance_testnet_api_secret: str = ""

    # Phase 2D (user directive, 2026-08-23) — PAPER/LIVE capital must never
    # be conflated. PAPER_CAPITAL is the existing $10,000 simulated global
    # pool (app.orchestration.global_allocator.global_allocator); it is
    # never read by anything live-execution-related.
    paper_capital_usd: float = 10_000.0

    # MICRO_LIVE_CAP — the ceiling app.execution.reality_quote applies on
    # top of whatever real balance Binance actually reports (never assumed
    # to already be 10 USDT). Checked independently of max_live_capital_usdt
    # below (defense in depth: two separately-enforced constants, one per
    # guard layer, so a bug in one path can't silently remove the cap).
    micro_live_cap_usdt: float = 10.0

    # Hard default OFF. While False, app.execution.live_guard refuses any
    # attempt to reach a real order-placement path — this is the single
    # switch that must flip before Phase 2D's next step (real order
    # authorization) can even be considered, and it is never flipped by
    # this codebase itself.
    live_trading_enabled: bool = False

    # Independent hard cap enforced by app.execution.live_guard at the
    # execution-attempt boundary itself, separate from micro_live_cap_usdt's
    # sizing-time check above.
    max_live_capital_usdt: float = 10.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
