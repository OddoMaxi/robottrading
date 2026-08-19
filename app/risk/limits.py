"""Risk limit configuration (section 23)."""

from dataclasses import dataclass


@dataclass(slots=True)
class RiskLimits:
    max_capital_per_trade_usd: float = 5_000
    max_exchange_exposure_pct: float = 50.0  # % of total portfolio on one exchange
    max_asset_exposure_pct: float = 40.0  # % of total portfolio in one asset
    max_daily_loss_usd: float = 500  # paper P&L in V1 — becomes real capital in Phase 2
    max_slippage_pct: float = 0.15
    min_liquidity_usd: float = 1_000  # minimum depth required at the test amount
    max_latency_ms: float = 500
