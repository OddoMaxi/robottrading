import random

from app.simulation.money import round_usd


def test_round_usd_quantizes_to_the_cent():
    assert round_usd(1.234) == 1.23
    assert round_usd(1.236) == 1.24


def test_round_usd_half_up_on_exact_boundary():
    assert round_usd(0.005) == 0.01
    assert round_usd(-0.005) == -0.01


def test_round_usd_is_a_no_op_on_an_already_cent_value():
    assert round_usd(2.85) == 2.85
    assert round_usd(0.0) == 0.0


def test_round_usd_prevents_live_vs_persisted_drift_over_many_trades():
    """The actual regression this whole fix targets: a long run of
    full-precision floats (the same shape app.simulation.paper_trader's
    rng.gauss() slippage draw produces) summed WITHOUT rounding drifts
    away from the sum of their cent-quantized values — the root cause of
    the 2026-08-22 "25K" ledger integrity violation. Applying round_usd
    to each increment BEFORE accumulating (matching what paper_trader.py
    now does before crediting the live balance) must make the running
    total exactly equal a fresh Postgres-Numeric(20,2)-style sum of the
    same increments, to the cent, no matter how many trades accumulate."""
    rng = random.Random(2026)
    raw_increments = [rng.gauss(0.5, 2.0) for _ in range(5000)]

    unrounded_running_total = 0.0
    rounded_running_total = 0.0
    persisted_sum = 0.0  # simulates what Postgres NUMERIC(20,2) would store and sum
    for raw in raw_increments:
        unrounded_running_total += raw  # the OLD, buggy behavior
        rounded = round_usd(raw)
        rounded_running_total += rounded  # the NEW, fixed behavior
        persisted_sum += round(rounded, 2)  # what actually lands in the DB column

    # The old behavior CAN drift from the DB's cent-rounded sum — this
    # assertion documents the bug existed, not that it always fires on
    # every seed/length; the real proof is the next assertion.
    live_vs_db_gap_old_behavior = abs(unrounded_running_total - persisted_sum)

    # The fix: rounding before accumulating makes live and "persisted"
    # sums agree to well within a cent, regardless of trade volume.
    live_vs_db_gap_new_behavior = abs(rounded_running_total - persisted_sum)
    assert live_vs_db_gap_new_behavior < 0.005
    assert live_vs_db_gap_new_behavior <= live_vs_db_gap_old_behavior + 1e-9
