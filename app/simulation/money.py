"""Deterministic cent-quantization for simulated CEX money values
(PRE-PHASE-2 CORRECTIVE MAINTENANCE, user directive, 2026-08-22).

ROOT CAUSE of the 2026-08-22 12:26:06 UTC ledger integrity violation on
the "25K" what-if portfolio (+$0.01 live-vs-DB disagreement): NOT
floating-point (IEEE-754) imprecision — that's ~1e-13, far below a cent.
The real cause is a ROUNDING SEMANTICS MISMATCH between two sources of
truth that are supposed to always agree:

  - the LIVE in-memory VirtualPortfolio.balances["USDT"] used to be
    credited with net_profit at FULL float precision (many decimal
    digits — it carries app.simulation.paper_trader's continuous
    rng.gauss() slippage draw, which is never a round number of cents);
  - the PERSISTED app.database.models.SimulatedTradeRecord.net_profit_usd
    column is Numeric(20, 2) — Postgres silently rounds every value to
    the nearest cent on write.

Summing many trades' worth of full-precision live increments against the
SAME trades' cent-rounded persisted increments diverges by more than a
cent after enough volume — confirmed by that "25K" was the portfolio with
enough trade volume this observation window to cross the $0.01 tolerance
first, not evidence of a data corruption or double-release bug.

The fix is not a wider tolerance (that hides the mismatch, it doesn't
close it) — it's to round net_profit to the cent, using Decimal
(deterministic, not float's imprecise rounding), at the ONE point a
trade's outcome becomes final, BEFORE it is either credited to the live
balance or persisted. Both then always agree exactly, because both are
handed the identical already-quantized value — this is the accounting
rule an exchange itself would apply (settlement happens in whole cents,
never fractions of a cent), made explicit and enforced here rather than
implicit and silently violated by the DB column's own rounding.
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def round_usd(value: float) -> float:
    """Quantizes a dollar amount to the nearest cent (ROUND_HALF_UP,
    matching ordinary decimal rounding conventions) via Decimal — the one
    deterministic rounding step every simulated money value must pass
    through before being credited to a live balance or persisted, so the
    two never drift apart. Decimal(str(value)), not Decimal(value)
    directly — constructing from the float's repr avoids importing
    IEEE-754's own binary representation noise into the quantization."""
    return float(Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP))
