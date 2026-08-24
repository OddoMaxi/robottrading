import pytest

from app.execution.candidate_selection import (
    CandidateClassification,
    CandidateInput,
    CandidateRejectionCache,
    CandidateStatus,
    classify_candidate,
    select_best_candidate,
)

# ---- shared "happy path" kwargs, overridden per test -----------------------

BASE_KWARGS = dict(
    quote_executable=True,
    quote_net_profit_usd=0.26,
    quote_executable_qty=3000.0,
    quote_reason=None,
    safety_margin_usd=0.0,
    buy_price=0.0032,
    sell_price=0.0032,
    buy_available_usdt=90.0,
    sell_available_qty=3000.0,
    buy_min_qty=0.1,
    sell_min_qty=0.1,
    buy_min_notional=5.0,
    sell_min_notional=1.0,
    buy_step_size=0.1,
    sell_step_size=0.1,
)


def _classify(**overrides):
    kwargs = dict(BASE_KWARGS)
    kwargs.update(overrides)
    return classify_candidate(**kwargs)


# ---- Test A (user directive, 2026-08-24): RVN exact real numbers -----------
# Bybit inventory = 84.5821 RVN, worth ~0.27 USDT at real observed prices --
# clears MIN_QTY comfortably but not Binance's real 5.0 USDT MIN_NOTIONAL.
# This is the exact scenario that burned 40/40 attempts in the invalidated
# batch. Expected: BELOW_MIN_NOTIONAL, and nothing downstream may ever call
# execute_one_arbitrage for it (asserted here via select_best_candidate
# returning None when it is the only candidate).


def test_a_rvn_exact_real_numbers_is_below_min_notional():
    result = _classify(sell_available_qty=84.5821, quote_executable_qty=3000.0, buy_price=0.0032, sell_price=0.0032)
    assert result.status == CandidateStatus.BELOW_MIN_NOTIONAL
    assert result.common_qty == pytest.approx(84.5)
    # ~0.27 USDT, as stated in the directive -- never enough to clear a 5.0 floor.
    assert result.common_qty * 0.0032 == pytest.approx(0.2704, abs=1e-3)


def test_a_rvn_below_min_notional_is_never_selectable():
    rvn = CandidateInput("RVN/USDT", "binance", "bybit", _classify(sell_available_qty=84.5821))
    assert select_best_candidate([rvn]) is None


# ---- Test B: best-edge candidate inexecutable, next one wins ---------------


def test_b_second_candidate_selected_when_best_edge_candidate_is_below_min_notional():
    rvn = CandidateInput("RVN/USDT", "binance", "bybit", _classify(sell_available_qty=84.5821))  # BELOW_MIN_NOTIONAL
    zil = CandidateInput("ZIL/USDT", "binance", "bybit", _classify(sell_available_qty=5000.0))  # EXECUTABLE_NOW
    selected = select_best_candidate([rvn, zil])  # ranked with RVN (better edge) first
    assert selected is not None
    assert selected.symbol == "ZIL/USDT"
    assert selected.classification.status == CandidateStatus.EXECUTABLE_NOW


# ---- Test C: two inexecutable, third is INVENTORY_MISSING but profitable ---


def test_c_inventory_missing_candidate_selected_for_automatic_constitution():
    a = CandidateInput("A/USDT", "binance", "bybit", _classify(sell_available_qty=1.0, buy_step_size=1.0, sell_step_size=1.0, buy_min_notional=100.0))  # BELOW_MIN_NOTIONAL
    b = CandidateInput("B/USDT", "binance", "bybit", _classify(quote_net_profit_usd=0.0))  # EDGE_TOO_LOW
    c = CandidateInput("C/USDT", "binance", "bybit", _classify(sell_available_qty=0.0))  # INVENTORY_MISSING, but edge is fine
    selected = select_best_candidate([a, b, c])
    assert selected is not None
    assert selected.symbol == "C/USDT"
    assert selected.classification.status == CandidateStatus.INVENTORY_MISSING


# ---- Test D: a structurally-rejected candidate must not be re-selected -----
# in a loop when nothing relevant has changed (the actual bug: the same
# candidate re-evaluated to the same answer 40 times running).


def test_d_unchanged_rejection_is_recognized_and_prevents_a_reselection_loop():
    cache = CandidateRejectionCache(ttl_seconds=60.0)
    signature = {"sell_available_qty": 84.5821, "regime": "CONFIRMED_SHORT_TERM", "edge_now": 26.17}

    assert cache.is_still_valid_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature) is False

    cache.record_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature)

    # Same candidate, same conditions, checked again immediately after (as
    # a batch loop scanning every few seconds would do) -- must be
    # recognized as still valid, so the caller skips it without
    # re-fetching/re-classifying, breaking the infinite reselection loop.
    assert cache.is_still_valid_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature) is True


def test_d_changed_inventory_invalidates_the_cached_rejection():
    cache = CandidateRejectionCache(ttl_seconds=60.0)
    signature = {"sell_available_qty": 84.5821, "regime": "CONFIRMED_SHORT_TERM", "edge_now": 26.17}
    cache.record_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature)

    # New inventory was constituted (or arrived) since the rejection --
    # the cache must not suppress a fresh, potentially-different verdict.
    changed_signature = {"sell_available_qty": 3000.0, "regime": "CONFIRMED_SHORT_TERM", "edge_now": 26.17}
    assert cache.is_still_valid_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", changed_signature) is False


def test_d_expired_ttl_invalidates_the_cached_rejection():
    cache = CandidateRejectionCache(ttl_seconds=30.0)
    signature = {"sell_available_qty": 84.5821, "regime": "CONFIRMED_SHORT_TERM", "edge_now": 26.17}
    cache.record_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature, now=1000.0)
    assert cache.is_still_valid_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature, now=1029.0) is True
    assert cache.is_still_valid_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "BELOW_MIN_NOTIONAL", signature, now=1031.0) is False


def test_d_clear_drops_every_cached_rejection_for_a_direction():
    cache = CandidateRejectionCache()
    signature = {"sell_available_qty": 0.0, "regime": "CONFIRMED_SHORT_TERM", "edge_now": 26.17}
    cache.record_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "INVENTORY_MISSING", signature)
    cache.clear("RVN/USDT", "BINANCE_BUY_BYBIT_SELL")
    assert cache.is_still_valid_rejection("RVN/USDT", "BINANCE_BUY_BYBIT_SELL", "INVENTORY_MISSING", signature) is False


# ---- classify_candidate: remaining branches (each of the 8 statuses) -------


def test_insufficient_depth_when_quote_not_executable():
    result = _classify(quote_executable=False, quote_reason="depth insufficient for requested size")
    assert result.status == CandidateStatus.INSUFFICIENT_DEPTH
    assert "depth" in result.reason


def test_edge_too_low_when_net_profit_does_not_clear_safety_margin():
    result = _classify(quote_net_profit_usd=0.05, safety_margin_usd=0.10)
    assert result.status == CandidateStatus.EDGE_TOO_LOW


def test_edge_too_low_when_net_profit_is_zero_or_negative():
    result = _classify(quote_net_profit_usd=-0.01)
    assert result.status == CandidateStatus.EDGE_TOO_LOW


def test_inventory_missing_when_sell_balance_is_zero():
    result = _classify(sell_available_qty=0.0)
    assert result.status == CandidateStatus.INVENTORY_MISSING


def test_inventory_missing_when_common_qty_rounds_to_zero():
    # Sell balance is nonzero but smaller than one step -- rounds down to 0.
    result = _classify(sell_available_qty=0.05, buy_step_size=1.0, sell_step_size=1.0)
    assert result.status == CandidateStatus.INVENTORY_MISSING


def test_below_min_notional_when_common_qty_under_min_qty():
    result = _classify(sell_available_qty=0.5, buy_step_size=0.1, sell_step_size=0.1, buy_min_qty=1.0, sell_min_qty=1.0)
    assert result.status == CandidateStatus.BELOW_MIN_NOTIONAL


def test_insufficient_balance_when_buy_side_usdt_too_low():
    result = _classify(buy_available_usdt=0.01, sell_available_qty=3000.0)
    assert result.status == CandidateStatus.INSUFFICIENT_BALANCE


def test_executable_now_happy_path():
    result = _classify(sell_available_qty=3000.0, buy_available_usdt=90.0)
    assert result.status == CandidateStatus.EXECUTABLE_NOW
    assert result.common_qty > 0


def test_common_qty_never_exceeds_real_sell_side_inventory():
    """Direct regression for the root cause: common_qty must be bounded
    by what the sell exchange actually holds, never by the buy-side
    reference quantity alone."""
    result = _classify(quote_executable_qty=100_000.0, sell_available_qty=84.5821, buy_step_size=0.1, sell_step_size=0.1)
    assert result.common_qty <= 84.5821


def test_select_best_candidate_returns_none_when_nothing_is_selectable():
    a = CandidateInput("A/USDT", "binance", "bybit", CandidateClassification(CandidateStatus.BELOW_MIN_NOTIONAL, "x", 0.0))
    b = CandidateInput("B/USDT", "binance", "bybit", CandidateClassification(CandidateStatus.EDGE_TOO_LOW, "x", 0.0))
    assert select_best_candidate([a, b]) is None


def test_select_best_candidate_prefers_first_selectable_in_ranked_order():
    a = CandidateInput("A/USDT", "binance", "bybit", CandidateClassification(CandidateStatus.INVENTORY_MISSING, "x", 0.0))
    b = CandidateInput("B/USDT", "binance", "bybit", CandidateClassification(CandidateStatus.EXECUTABLE_NOW, "x", 10.0))
    selected = select_best_candidate([a, b])
    assert selected is not None and selected.symbol == "A/USDT"
