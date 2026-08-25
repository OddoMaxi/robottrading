"""REAL NET WORTH REPORTING (user directive, 2026-08-25, V5 item 5 --
"REAL NET WORTH AS PRIMARY KPI"). LIQUIDATION_NET_WORTH, computed from
real balances and real bid prices, becomes the primary KPI; the old V4
dashboard could show a TRUE_SESSION_NET_PNL that never reconciled with
actual patrimony (the forensic reconstruction found a $31.16 gap between
the two) -- this module makes that gap a first-class, always-checked
number instead of something that has to be discovered after the fact.

Pure functions only; every balance/price is caller-supplied (real account
reads happen at the edges, in the orchestrator/dashboard, exactly like
every other module in this codebase)."""

from dataclasses import dataclass

QUOTE_ASSET = "USDT"


def compute_liquidation_net_worth(
    balances: dict[str, float], bid_prices: dict[str, float], *, liquidation_fee_rate: float = 0.0,
) -> float:
    """Pure. Sum of qty * real_bid_price, net of an optional disclosed
    liquidation-fee assumption (0.0 by default -- callers wanting the
    conservative "what I could realistically get" framing pass e.g.
    0.001 for a standard 0.1% taker fee; the forensic reconstruction used
    0.0 for its closing wealth-bridge check specifically because real,
    per-trade fees are already fully captured elsewhere and a synthetic
    assumption on top would be double-counting). USDT itself is never
    haircut -- it is already liquid."""
    total = 0.0
    for asset, qty in balances.items():
        if qty <= 0:
            continue
        if asset == QUOTE_ASSET:
            total += qty
            continue
        price = bid_prices.get(asset)
        if price is None:
            continue
        total += qty * price * (1.0 - liquidation_fee_rate)
    return total


def compute_real_wealth_pnl(starting_net_worth_usd: float, current_net_worth_usd: float) -> float:
    return current_net_worth_usd - starting_net_worth_usd


def compute_real_wealth_return_pct(starting_net_worth_usd: float, current_net_worth_usd: float) -> float | None:
    """None (never a fabricated 0.0 or inf) when starting net worth is
    not positive -- a return percentage is undefined there."""
    if starting_net_worth_usd <= 0:
        return None
    return (current_net_worth_usd - starting_net_worth_usd) / starting_net_worth_usd * 100.0


@dataclass(slots=True, frozen=True)
class ReconciliationCheck:
    accounting_pnl_usd: float
    real_wealth_change_usd: float
    gap_usd: float
    tolerance_usd: float
    within_tolerance: bool


def check_reconciliation_invariant(
    accounting_pnl_usd: float, real_wealth_change_usd: float, *, tolerance_usd: float = 0.01,
) -> ReconciliationCheck:
    """Pure. The mandatory invariant (item 5, user directive):
    ABS(ACCOUNTING_PNL - REAL_WEALTH_CHANGE) <= tolerance. Any dashboard
    or session report displaying a P&L number is required to run this
    check against the same session's real wealth change before display;
    a caller finding within_tolerance=False must never show the
    accounting number as if it were reconciled."""
    gap = accounting_pnl_usd - real_wealth_change_usd
    return ReconciliationCheck(
        accounting_pnl_usd=accounting_pnl_usd, real_wealth_change_usd=real_wealth_change_usd, gap_usd=gap,
        tolerance_usd=tolerance_usd, within_tolerance=abs(gap) <= tolerance_usd,
    )
