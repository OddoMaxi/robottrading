"""Flash Loan Research Engine (Multi-Market Opportunity Engine, V5.5, spec
sections 9-11).

PAPER / SIMULATION ONLY (spec's own section heading, restated in section
33: "No real flash-loan execution"). This module never borrows, never
repays, never touches a lending protocol — it answers a research
question: "if a flash loan funded a much larger size than our own capital
ever could, for the duration of one atomic transaction, would the
resulting arbitrage still generate enough to repay principal + the loan's
own fee, and keep a profit on top?"

Architecture (section 9): Flash Loan -> Swap A -> Swap B / Multi-hop ->
Repay Loan -> Profit, modeled as a single atomic bundle (section 11:
"borrow -> swaps -> repay -> commit... if any condition fails: REVERT" —
implemented literally as the `repayable` gate below: an unrepayable
simulation is never counted as a profitable opportunity, exactly the "do
not count expected profits from partially successful atomic simulations"
instruction).

Aave v3's flash loan fee (0.05% of the borrowed amount) is real, public,
documented protocol data — not fabricated. Flash loans are EVM-only here:
Solana has no comparably standardized, widely-adopted flash-loan-lending
protocol at the time of writing, so this module doesn't fabricate a
Solana equivalent rather than inventing one.
"""

import time
import uuid
from dataclasses import dataclass

from app.config.constants import Strategy
from app.onchain.constants import NOMINAL_DEX_HOLDING_SECONDS
from app.onchain.cross_dex_arbitrage import DexTierResult, evaluate_dex_capital_tier
from app.onchain.models import DexPool
from app.opportunity.models import Opportunity

# Real, public, documented Aave v3 flash loan fee.
AAVE_V3_FLASH_LOAN_FEE_PCT = 0.05

# Larger than app.onchain.constants.DEX_CAPITAL_TEST_TIERS_USD's own-capital
# ladder — spec section 16: "For flash-loan research also test larger
# amounts" — sizes no real own-capital portfolio in this system could ever
# deploy, which is exactly the point of researching whether a flash loan's
# access to size is worth its fee.
FLASH_LOAN_TEST_AMOUNTS_USD = [10_000.0, 25_000.0, 50_000.0, 100_000.0, 250_000.0, 500_000.0]

FLASH_LOAN_EVM_CHAINS = {"eth", "bsc"}


@dataclass(slots=True)
class FlashLoanResult:
    borrowed_capital_usd: float
    # swap fees, gas, AMM price impact, slippage buffer, and MEV buffer are
    # already netted into this figure by the reused
    # app.onchain.cross_dex_arbitrage.evaluate_dex_capital_tier — not
    # duplicated or re-broken-out here, see that module for the breakdown.
    arbitrage_net_profit_usd: float
    flash_loan_fee_usd: float
    final_net_profit_usd: float
    repayable: bool  # spec section 9's own rule: unrepayable = NOT EXECUTABLE


def simulate_flash_loan_tier(
    buy_pool: DexPool, sell_pool: DexPool, buy_price: float, sell_price: float, gas_cost_usd: float, borrowed_capital_usd: float
) -> FlashLoanResult:
    tier: DexTierResult = evaluate_dex_capital_tier(buy_pool, sell_pool, buy_price, sell_price, borrowed_capital_usd, gas_cost_usd)
    flash_loan_fee_usd = borrowed_capital_usd * (AAVE_V3_FLASH_LOAN_FEE_PCT / 100)
    final_net_profit_usd = tier.net_profit_usd - flash_loan_fee_usd

    # Transaction Atomicity (spec section 11) — the resulting balance after
    # every swap (tier.net_profit_usd already includes swap fees, gas,
    # price impact, and slippage/MEV buffers) must cover principal + the
    # loan's own fee, or this bundle would revert on-chain; a simulation
    # that can't repay is never counted as a profitable opportunity.
    resulting_balance_usd = borrowed_capital_usd + tier.net_profit_usd
    repayable = resulting_balance_usd >= (borrowed_capital_usd + flash_loan_fee_usd)

    return FlashLoanResult(
        borrowed_capital_usd=borrowed_capital_usd,
        arbitrage_net_profit_usd=tier.net_profit_usd,
        flash_loan_fee_usd=flash_loan_fee_usd,
        final_net_profit_usd=final_net_profit_usd,
        repayable=repayable,
    )


def find_best_flash_loan_size(
    buy_pool: DexPool,
    sell_pool: DexPool,
    buy_price: float,
    sell_price: float,
    gas_cost_usd: float,
    test_amounts_usd: list[float] = FLASH_LOAN_TEST_AMOUNTS_USD,
) -> FlashLoanResult | None:
    """The size (among the tested ladder) that maximizes real dollar
    profit AMONG repayable results only — an unrepayable size is never a
    candidate, regardless of its theoretical final_net_profit_usd number."""
    results = [
        simulate_flash_loan_tier(buy_pool, sell_pool, buy_price, sell_price, gas_cost_usd, amount) for amount in test_amounts_usd
    ]
    repayable_results = [r for r in results if r.repayable and r.final_net_profit_usd > 0]
    if not repayable_results:
        return None
    return max(repayable_results, key=lambda r: r.final_net_profit_usd)


@dataclass(slots=True)
class FlashLoanComparisonResult:
    """Flash Loan Economic Value (spec section 10) — own capital never
    beats flash loan just by assumption; this compares the two real
    numbers and lets the better one win."""

    own_capital_usd: float
    own_capital_net_profit_usd: float
    flash_loan_borrowed_usd: float | None
    flash_loan_net_profit_usd: float | None
    flash_loan_is_superior: bool


def build_flash_loan_opportunity(
    buy_pool: DexPool, sell_pool: DexPool, result: FlashLoanResult, theoretical_edge_pct: float
) -> Opportunity:
    """Mirrors app.onchain.cross_dex_arbitrage.detect_cross_dex_opportunity's
    own Opportunity construction — same shared type (spec section 1's
    Opportunity Bus), tagged Strategy.FLASH_LOAN_RESEARCH so it's never
    confused with an own-capital execution."""
    net_pct = (result.final_net_profit_usd / result.borrowed_capital_usd * 100) if result.borrowed_capital_usd else 0.0
    return Opportunity(
        strategy=Strategy.FLASH_LOAN_RESEARCH,
        symbol=f"{buy_pool.token0_symbol.upper()}/{buy_pool.token1_symbol.upper()}",
        legs=[
            {"chain": buy_pool.chain, "exchange": buy_pool.dex, "side": "buy", "market": "dex", "pool_id": buy_pool.pool_id},
            {"chain": sell_pool.chain, "exchange": sell_pool.dex, "side": "sell", "market": "dex", "pool_id": sell_pool.pool_id},
        ],
        gross_spread_pct=theoretical_edge_pct,
        net_spread_pct=net_pct,
        capital_usd=result.borrowed_capital_usd,
        expected_profit_usd=result.final_net_profit_usd,
        market_data_age_seconds=max(0.0, time.time() - max(buy_pool.last_update, sell_pool.last_update)),
        holding_period_seconds=NOMINAL_DEX_HOLDING_SECONDS,
        theoretical_edge_pct=theoretical_edge_pct,
        depth_adjusted_edge_pct=net_pct,
        realistic_executable_edge_pct=net_pct,
        optimal_capital_usd=result.borrowed_capital_usd,
        max_profitable_capital_usd=None,
        capital_is_liquidity_capped=True,
        detected_at=time.time(),
        id=uuid.uuid4(),
    )


def compare_own_capital_vs_flash_loan(
    own_capital_result: DexTierResult, flash_loan_result: FlashLoanResult | None
) -> FlashLoanComparisonResult:
    if flash_loan_result is None:
        return FlashLoanComparisonResult(
            own_capital_usd=own_capital_result.capital_usd,
            own_capital_net_profit_usd=own_capital_result.net_profit_usd,
            flash_loan_borrowed_usd=None,
            flash_loan_net_profit_usd=None,
            flash_loan_is_superior=False,
        )
    return FlashLoanComparisonResult(
        own_capital_usd=own_capital_result.capital_usd,
        own_capital_net_profit_usd=own_capital_result.net_profit_usd,
        flash_loan_borrowed_usd=flash_loan_result.borrowed_capital_usd,
        flash_loan_net_profit_usd=flash_loan_result.final_net_profit_usd,
        flash_loan_is_superior=flash_loan_result.final_net_profit_usd > own_capital_result.net_profit_usd,
    )
