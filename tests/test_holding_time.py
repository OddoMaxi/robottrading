from app.config.constants import HoldingTimeCategory
from app.opportunity.holding_time import classify_holding_time, is_fast_mode


def test_ultra_fast_boundary():
    assert classify_holding_time(5.0) == HoldingTimeCategory.ULTRA_FAST
    assert classify_holding_time(29.9) == HoldingTimeCategory.ULTRA_FAST


def test_fast_boundary():
    assert classify_holding_time(30.0) == HoldingTimeCategory.FAST
    assert classify_holding_time(299.0) == HoldingTimeCategory.FAST


def test_medium_boundary():
    assert classify_holding_time(300.0) == HoldingTimeCategory.MEDIUM
    assert classify_holding_time(1_799.0) == HoldingTimeCategory.MEDIUM


def test_carry_boundary():
    assert classify_holding_time(1_800.0) == HoldingTimeCategory.CARRY
    assert classify_holding_time(3 * 86_400) == HoldingTimeCategory.CARRY  # funding, 3 days
    assert classify_holding_time(36 * 86_400) == HoldingTimeCategory.CARRY  # basis, 36 days


def test_is_fast_mode():
    assert is_fast_mode(8.0) is True
    assert is_fast_mode(1_799.0) is True
    assert is_fast_mode(1_800.0) is False
    assert is_fast_mode(36 * 86_400) is False
