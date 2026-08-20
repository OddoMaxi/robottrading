"""Risk limit configuration (section 23; % cap is Fast-Rotation spec section 31)."""

from dataclasses import dataclass


@dataclass(slots=True)
class RiskLimits:
    # Effective per-trade cap is min(portfolio_value * max_capital_per_trade_pct,
    # max_capital_per_trade_usd) — the % scales down naturally for small
    # portfolios and up for large ones; the flat USD figure is a hard ceiling
    # regardless of portfolio size.
    max_capital_per_trade_pct: float = 20.0
    max_capital_per_trade_usd: float = 5_000
    max_exchange_exposure_pct: float = 50.0  # % of total portfolio on one exchange
    max_asset_exposure_pct: float = 40.0  # % of total portfolio in one asset
    max_daily_loss_usd: float = 500  # paper P&L in V1 — becomes real capital in Phase 2
    max_slippage_pct: float = 0.15
    min_liquidity_usd: float = 1_000  # minimum depth required at the test amount
    max_latency_ms: float = 500

    def max_capital_for_portfolio(self, portfolio_value_usd: float) -> float:
        return min(portfolio_value_usd * self.max_capital_per_trade_pct / 100, self.max_capital_per_trade_usd)
