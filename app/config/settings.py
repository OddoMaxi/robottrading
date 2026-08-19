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


@lru_cache
def get_settings() -> Settings:
    return Settings()
