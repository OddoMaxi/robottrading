"""REJECTION CLASSIFICATION (user directive, 2026-08-25, "MISSION --
CONTINUOUS OKX TRUE-ECONOMIC VALIDATION", item 3: "Do not let one
blocker dominate" / item 4: "cost basis problem"). A continuous scanner
that only ever reports "NOT_TRUE_ECONOMIC_POSITIVE" for every rejected
candidate is useless for diagnosing WHY OKX opportunities aren't
converting -- this module gives every rejection ONE specific, honestly-
derived reason, reusing the exact fields app.execution.dual_leg_quote.
DualLegQuote and app.execution.true_economic_pretrade.TrueEconomicQuote/
ExecutabilityCheck already compute (never re-deriving book/fee/depth
logic, never inventing a distinction the upstream data can't actually
support).

Priority order matters: a candidate is classified by the FIRST
structural reason that would prevent it trading at all, before economic
questions (is the edge positive) are even asked -- a route that fails
min_notional is not "not true economic positive", it never got a valid
quote to evaluate in the first place."""

from dataclasses import dataclass

from app.execution.dual_leg_quote import DualLegQuote
from app.execution.true_economic_ledger import CostBasisPool
from app.execution.true_economic_pretrade import ExecutabilityCheck, TrueEconomicQuote

REJECTION_TAXONOMY = (
    "NOT_TRADABLE",
    "BELOW_MIN_NOTIONAL",
    "INSUFFICIENT_DEPTH",
    "UNKNOWN_SELL_COST_BASIS",
    "INSUFFICIENT_CAPITAL",
    "RESERVE_FLOOR",
    "INSUFFICIENT_SELL_INVENTORY",
    "NOT_TRUE_ECONOMIC_POSITIVE",
    "OTHER",
)

DEPTH_INSUFFICIENT_SLIPPAGE_PCT_THRESHOLD = 100.0


def classify_rejection(
    *,
    quote: DualLegQuote,
    te_quote: TrueEconomicQuote,
    executability: ExecutabilityCheck,
    real_balance_before_floor_usd: float,
    reserve_floor_usd: float,
) -> str:
    """Pure. real_balance_before_floor_usd is the buy exchange's RAW real
    balance (before subtracting the reserve floor) -- this is what lets
    RESERVE_FLOOR be told apart from genuine INSUFFICIENT_CAPITAL: a
    candidate that would be affordable out of the raw balance but not
    out of (balance - floor) is being blocked by the safety buffer, not
    by an empty account."""
    if not quote.buy_tradable or not quote.sell_tradable:
        return "NOT_TRADABLE"

    if not quote.buy_lot_size_pass or not quote.sell_lot_size_pass or not quote.buy_min_notional_pass or not quote.sell_min_notional_pass:
        return "BELOW_MIN_NOTIONAL"

    if quote.buy_slippage_pct >= DEPTH_INSUFFICIENT_SLIPPAGE_PCT_THRESHOLD or quote.sell_slippage_pct >= DEPTH_INSUFFICIENT_SLIPPAGE_PCT_THRESHOLD:
        return "INSUFFICIENT_DEPTH"

    if te_quote.sell_inventory_cost_basis_usd is None:
        return "UNKNOWN_SELL_COST_BASIS"

    if not executability.capital_sufficient:
        if real_balance_before_floor_usd >= executability.capital_required_usd and reserve_floor_usd > 0:
            return "RESERVE_FLOOR"
        return "INSUFFICIENT_CAPITAL"

    if not executability.inventory_sufficient:
        return "INSUFFICIENT_SELL_INVENTORY"

    if not executability.true_economic_positive:
        return "NOT_TRUE_ECONOMIC_POSITIVE"

    return "OTHER"  # executable_now should be True at this point; reaching OTHER signals a gap in this taxonomy, not a fabricated label


@dataclass(slots=True, frozen=True)
class CostBasisGapDiagnosis:
    exchange: str
    asset: str
    ledger_qty: float
    real_balance_qty: float
    category: str  # one of: ZERO_REAL_BALANCE, LEDGER_MATCHES_REAL_BALANCE, LEDGER_UNDERSTATES_REAL_BALANCE, LEDGER_OVERSTATES_REAL_BALANCE
    detail: str


RECONCILIATION_TOLERANCE = 1e-6


def diagnose_cost_basis_gap(pool: CostBasisPool, real_balance_qty: float) -> CostBasisGapDiagnosis:
    """Pure. Answers the mission's item-4 question for one (exchange,
    asset): why is the sell-side cost basis unknown or insufficient,
    classified strictly from real, caller-supplied numbers (pool.qty is
    this session's own ledger; real_balance_qty is a fresh real account
    read) -- never inferred from current market price, matching the
    standing "never fabricate a cost basis" rule."""
    if real_balance_qty <= 1e-9:
        return CostBasisGapDiagnosis(
            exchange=pool.exchange, asset=pool.asset, ledger_qty=pool.qty, real_balance_qty=real_balance_qty,
            category="ZERO_REAL_BALANCE",
            detail=f"{pool.exchange} genuinely holds no {pool.asset} right now -- there is nothing to sell, not a tracking gap.",
        )
    diff = real_balance_qty - pool.qty
    if abs(diff) <= RECONCILIATION_TOLERANCE * max(1.0, real_balance_qty):
        return CostBasisGapDiagnosis(
            exchange=pool.exchange, asset=pool.asset, ledger_qty=pool.qty, real_balance_qty=real_balance_qty,
            category="LEDGER_MATCHES_REAL_BALANCE",
            detail="Ledger quantity matches the real account balance -- cost basis is known and authoritative for the full real position.",
        )
    if diff > 0:
        return CostBasisGapDiagnosis(
            exchange=pool.exchange, asset=pool.asset, ledger_qty=pool.qty, real_balance_qty=real_balance_qty,
            category="LEDGER_UNDERSTATES_REAL_BALANCE",
            detail=(
                f"Real balance ({real_balance_qty}) exceeds what this session's ledger has tracked ({pool.qty}) by {diff:.8f} "
                f"{pool.asset} -- these units were acquired outside this session (before ledger seeding, or by a process this "
                "ledger never observed). Their true acquisition cost is unavailable; they remain untradeable under the "
                "true-economic gate until a real fill establishes their cost, never inferred from current price."
            ),
        )
    return CostBasisGapDiagnosis(
        exchange=pool.exchange, asset=pool.asset, ledger_qty=pool.qty, real_balance_qty=real_balance_qty,
        category="LEDGER_OVERSTATES_REAL_BALANCE",
        detail=(
            f"Ledger tracks more {pool.asset} ({pool.qty}) than the real account actually holds ({real_balance_qty}) by "
            f"{-diff:.8f} -- a reconciliation anomaly (asset moved off-ledger, e.g. a withdrawal or a trade this ledger never "
            "recorded). The real order path independently caps every sell by the real account balance, so this cannot cause "
            "an oversell, but the ledger's own PNL accounting for this asset is stale and should not be trusted until reseeded."
        ),
    )
