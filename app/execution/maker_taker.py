"""Maker/Taker Strategy Engine (Net Opportunity Engine spec, section 4).

Evaluates all 4 execution modes for a 2-leg cross-exchange trade and picks
the best by expected value.

Execution is modeled sequentially, never simultaneously: a maker leg is
placed and waited on FIRST; the paired leg only fires once the maker leg
fills. This means a lone unfilled maker leg costs nothing — the trade is
simply abandoned, no naked exposure. The only real risk modeled is in
Maker/Maker, where the *second* maker leg can fail to fill after the first
already has, leaving a position that must be flattened at market.
"""

from dataclasses import dataclass
from enum import StrEnum

from app.analytics.fees import FeeEngine
from app.config.constants import MarketType

# Cost assumption if the second maker leg in Maker/Maker fails to fill after
# the first already has — unvalidated, same caveat as fill_probability.py.
DEFAULT_ADVERSE_MOVE_PCT = 0.05


class ExecutionMode(StrEnum):
    TAKER_TAKER = "taker_taker"
    MAKER_TAKER = "maker_taker"
    TAKER_MAKER = "taker_maker"
    MAKER_MAKER = "maker_maker"


@dataclass(slots=True)
class ExecutionModeResult:
    mode: ExecutionMode
    fill_probability: float  # probability the trade executes at all
    net_profit_if_filled_usd: float  # profit in the scenario where it does
    expected_value_usd: float  # probability-weighted — what actually drives the ranking
    buy_price: float
    sell_price: float


def evaluate_execution_modes(
    buy_exchange: str,
    sell_exchange: str,
    buy_bid: float,
    buy_ask: float,
    sell_bid: float,
    sell_ask: float,
    capital_usd: float,
    fee_engine: FeeEngine,
    buy_maker_fill_probability: float,
    sell_maker_fill_probability: float,
    adverse_move_pct: float = DEFAULT_ADVERSE_MOVE_PCT,
) -> list[ExecutionModeResult]:
    results: list[ExecutionModeResult] = []

    # TAKER/TAKER — both legs cross the book immediately, always executes.
    quantity_tt = capital_usd / buy_ask
    buy_fee_tt = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital_usd, is_maker=False)
    sell_fee_tt = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, quantity_tt * sell_bid, is_maker=False)
    profit_tt = quantity_tt * (sell_bid - buy_ask) - buy_fee_tt - sell_fee_tt
    results.append(ExecutionModeResult(ExecutionMode.TAKER_TAKER, 1.0, profit_tt, profit_tt, buy_ask, sell_bid))

    # MAKER/TAKER — buy rests at the bid first; the taker sell only fires once it fills.
    quantity_mt = capital_usd / buy_bid
    buy_fee_mt = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital_usd, is_maker=True)
    sell_fee_mt = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, quantity_mt * sell_bid, is_maker=False)
    profit_mt = quantity_mt * (sell_bid - buy_bid) - buy_fee_mt - sell_fee_mt
    results.append(
        ExecutionModeResult(ExecutionMode.MAKER_TAKER, buy_maker_fill_probability, profit_mt, buy_maker_fill_probability * profit_mt, buy_bid, sell_bid)
    )

    # TAKER/MAKER — sell rests at the ask first; the taker buy only fires once it fills.
    quantity_tm = capital_usd / buy_ask
    buy_fee_tm = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital_usd, is_maker=False)
    sell_fee_tm = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, quantity_tm * sell_ask, is_maker=True)
    profit_tm = quantity_tm * (sell_ask - buy_ask) - buy_fee_tm - sell_fee_tm
    results.append(
        ExecutionModeResult(ExecutionMode.TAKER_MAKER, sell_maker_fill_probability, profit_tm, sell_maker_fill_probability * profit_tm, buy_ask, sell_ask)
    )

    # MAKER/MAKER — buy rests first; only once it fills does the sell leg rest too.
    # If that second leg then fails to fill, the buy position must be flattened at market.
    quantity_mm = capital_usd / buy_bid
    buy_fee_mm = fee_engine.trading_fee(buy_exchange, MarketType.SPOT, capital_usd, is_maker=True)
    sell_fee_mm = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, quantity_mm * sell_ask, is_maker=True)
    both_fill_profit = quantity_mm * (sell_ask - buy_bid) - buy_fee_mm - sell_fee_mm
    flatten_fee = fee_engine.trading_fee(sell_exchange, MarketType.SPOT, quantity_mm * sell_bid, is_maker=False)
    naked_cost = -(capital_usd * adverse_move_pct / 100) - flatten_fee
    p_buy, p_sell = buy_maker_fill_probability, sell_maker_fill_probability
    ev_mm = p_buy * (p_sell * both_fill_profit + (1 - p_sell) * naked_cost)
    results.append(ExecutionModeResult(ExecutionMode.MAKER_MAKER, p_buy * p_sell, both_fill_profit, ev_mm, buy_bid, sell_ask))

    return results


def best_execution_mode(results: list[ExecutionModeResult]) -> ExecutionModeResult:
    return max(results, key=lambda r: r.expected_value_usd)
