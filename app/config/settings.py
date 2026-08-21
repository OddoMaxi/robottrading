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

    # Exchange API credentials (public market data only in V1 — kept for Phase 2)
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
