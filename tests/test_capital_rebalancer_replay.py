"""31-EVENT END-TO-END REPLAY (user directive, 2026-08-25) -- permanent
regression proving the capital rebalancer would have prevented the real
2026-08-24 incident (Binance USDT drained from ~72.79 to 2.66 over 10
consecutive same-direction RVN cycles) from recurring uncaught.

The 31 events below are the EXACT real historical data (15 real
arbitrage buy legs + 16 real inventory constitutions, all outcome
both_filled/filled) fetched directly from the production ledger on
2026-08-25 -- not synthetic data. See app.execution.capital_rebalancer.
simulate_event_sequence's own docstring for the replay methodology and
its one disclosed limitation (opposite-direction preference is not
modeled, making this a lower bound on the real integrated benefit).
"""

from app.execution.capital_rebalancer import ReplayEvent, compute_reserve_floor, simulate_event_sequence

# (started_at_iso, kind, symbol, spend_exchange, receive_exchange, spend_notional_usdt,
#  receive_notional_usdt, price_usdt, qty_received_net, qty_sold)
_RAW_EVENTS = [
    ("2026-08-24T17:25:09.365468", "INVENTORY", "RVNUSDT", "bybit", None, 9.9996433, 0.0, 0.003427, 2917.9, 0.0),
    ("2026-08-24T22:25:34.622642", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 9.34989, 9.609208, 0.0033, 2830.4667, 2830.4),
    ("2026-08-24T23:01:30.569406", "INVENTORY", "SANDUSDT", "binance", None, 9.99192, 0.0, 0.04216, 237.0, 0.0),
    ("2026-08-24T23:01:34.405274", "ARBITRAGE", "SANDUSDT", "bybit", "binance", 9.70678, 9.7788, 0.04166, 232.767, 232.0),
    ("2026-08-24T23:22:07.568374", "INVENTORY", "ZILUSDT", "bybit", None, 9.999821, 0.0, 0.002762, 3616.8795, 0.0),
    ("2026-08-24T23:22:11.272687", "ARBITRAGE", "ZILUSDT", "binance", "bybit", 9.7378172, 9.8457674, 0.002726, 3568.6278, 3568.6),
    ("2026-08-24T23:22:21.747537", "INVENTORY", "LUNCUSDT", "bybit", None, 9.9995735, 0.0, 5.41e-05, 184650.165, 0.0),
    ("2026-08-24T23:22:25.540042", "ARBITRAGE", "LUNCUSDT", "binance", "bybit", 9.86436125, 9.916806509999999, 5.375e-05, 183339.477, 183339.0),
    ("2026-08-24T23:22:39.540995", "INVENTORY", "MANTRAUSDT", "binance", None, 9.99972, 0.0, 0.00423, 2361.636, 0.0),
    ("2026-08-24T23:22:43.607572", "ARBITRAGE", "MANTRAUSDT", "bybit", "binance", 9.856904, 9.8959, 0.004198, 2345.652, 2345.0),
    ("2026-08-24T23:57:41.435148", "INVENTORY", "RVNUSDT", "bybit", None, 7.209763000000001, 0.0, 0.00341, 2112.1857, 0.0),
    ("2026-08-24T23:57:45.247464", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.0214099999999995, 7.2415785, 0.0033, 2125.5723, 2125.5),
    ("2026-08-24T23:57:52.723076", "INVENTORY", "RVNUSDT", "bybit", None, 7.2598063999999995, 0.0, 0.003409, 2127.4704, 0.0),
    ("2026-08-24T23:57:56.558003", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.0257, 7.2481344000000005, 0.0033, 2126.871, 2126.8),
    ("2026-08-24T23:58:04.485007", "INVENTORY", "RVNUSDT", "bybit", None, 7.249920299999999, 0.0, 0.003409, 2124.5733, 0.0),
    ("2026-08-24T23:58:08.196361", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.02075, 7.240897100000001, 0.0033, 2125.3725, 2125.3),
    ("2026-08-24T23:58:15.348954", "INVENTORY", "RVNUSDT", "bybit", None, 7.2598709999999995, 0.0, 0.003414, 2124.3735, 0.0),
    ("2026-08-24T23:58:19.038528", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.00722, 7.2375343999999995, 0.0033, 2121.2766, 2121.2),
    ("2026-08-24T23:58:26.555393", "INVENTORY", "RVNUSDT", "bybit", None, 7.2499704, 0.0, 0.003414, 2121.4764, 0.0),
    ("2026-08-24T23:58:30.254945", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.0059, 7.2404112000000005, 0.0033, 2120.877, 2120.8),
    ("2026-08-24T23:58:37.761211", "INVENTORY", "RVNUSDT", "bybit", None, 7.2397284, 0.0, 0.003414, 2118.4794, 0.0),
    ("2026-08-24T23:58:41.997393", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.0006200000000005, 7.232829599999999, 0.0033, 2119.2786, 2119.2),
    ("2026-08-24T23:58:49.600500", "INVENTORY", "RVNUSDT", "bybit", None, 7.249703500000001, 0.0, 0.003415, 2120.7771, 0.0),
    ("2026-08-24T23:58:54.002156", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.00194, 7.2363143999999995, 0.0033, 2119.6782, 2119.6),
    ("2026-08-24T23:59:01.275480", "INVENTORY", "RVNUSDT", "bybit", None, 7.2398, 0.0, 0.003415, 2117.88, 0.0),
    ("2026-08-24T23:59:05.532585", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 6.9959999999999996, 7.230169200000001, 0.0033, 2117.88, 2117.8),
    ("2026-08-24T23:59:13.124893", "INVENTORY", "RVNUSDT", "bybit", None, 7.239732500000001, 0.0, 0.003419, 2115.3825, 0.0),
    ("2026-08-24T23:59:17.122764", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.030771, 7.2441666, 0.00331, 2121.9759, 2121.9),
    ("2026-08-24T23:59:24.762074", "INVENTORY", "RVNUSDT", "bybit", None, 7.2598709999999995, 0.0, 0.003414, 2124.3735, 0.0),
    ("2026-08-24T23:59:28.444260", "ARBITRAGE", "RVNUSDT", "binance", "bybit", 7.02174, 7.2461704, 0.0033, 2125.6722, 2125.6),
    ("2026-08-24T23:59:36.048519", "INVENTORY", "RVNUSDT", "bybit", None, 7.2699484, 0.0, 0.003412, 2128.5693, 0.0),
]

REAL_EVENTS = [
    ReplayEvent(
        at=at, kind=kind, symbol=symbol, spend_exchange=spend_exch, receive_exchange=recv_exch,
        spend_notional_usdt=spend_notional, receive_notional_usdt=recv_notional, price_usdt=price,
        qty_received_net=qty_recv, qty_sold=qty_sold,
    )
    for at, kind, symbol, spend_exch, recv_exch, spend_notional, recv_notional, price, qty_recv, qty_sold in _RAW_EVENTS
]

# The historically-intended target split (settings.py: binance_target_capital_usdt=100,
# bybit_target_capital_usdt=60) -- the exact real starting balance before this
# specific sequence began cannot be reconstructed after the fact.
STARTING_BINANCE_USDT = 100.0
STARTING_BYBIT_USDT = 60.0
FLOOR = compute_reserve_floor(10.0)  # the deployed MAX_NOTIONAL_PER_LEG_USDT at the time


def test_all_31_real_events_are_present():
    assert len(REAL_EVENTS) == 31
    assert sum(1 for e in REAL_EVENTS if e.kind == "ARBITRAGE") == 15
    assert sum(1 for e in REAL_EVENTS if e.kind == "INVENTORY") == 16


def test_old_logic_reproduces_the_real_danger_zone():
    """Sanity check on the fixture itself: replaying these 31 real events
    with NO reserve check (floor set to -infinity, i.e. never trips)
    must independently reproduce the same danger zone observed in
    reality (Binance draining toward zero) -- otherwise the fixture
    would not actually be exercising the real incident's shape."""
    result = simulate_event_sequence(
        REAL_EVENTS, starting_binance_usdt=STARTING_BINANCE_USDT, starting_bybit_usdt=STARTING_BYBIT_USDT,
        binance_floor=-1e9, bybit_floor=-1e9,
    )
    assert result.interventions == 0  # floor never trips at -inf -- confirms this run is genuinely the "old" baseline
    assert result.min_binance_usdt < 5.0  # same danger zone as the real observed 2.66 USDT


def test_31_event_end_to_end_replay_never_breaches_the_binance_floor():
    """THE regression this file exists for: with the real reserve floor
    (25.0 USDT at the deployed 10 USDT/leg cap) wired in, the exact same
    31 real events must never leave Binance meaningfully below its
    floor. A small, disclosed taker-fee-driven slack (well under $1) is
    expected and acceptable -- REBALANCE_FIRST restores headroom to
    just above the floor, not to some larger buffer."""
    result = simulate_event_sequence(
        REAL_EVENTS, starting_binance_usdt=STARTING_BINANCE_USDT, starting_bybit_usdt=STARTING_BYBIT_USDT,
        binance_floor=FLOOR, bybit_floor=FLOOR,
    )
    assert result.min_binance_usdt >= FLOOR - 0.5
    assert result.min_bybit_usdt >= FLOOR - 0.5
    assert result.interventions > 0  # confirms the floor genuinely had to act, not a vacuous pass


def test_31_event_replay_min_binance_balance_is_far_above_the_real_2_66_incident():
    result = simulate_event_sequence(
        REAL_EVENTS, starting_binance_usdt=STARTING_BINANCE_USDT, starting_bybit_usdt=STARTING_BYBIT_USDT,
        binance_floor=FLOOR, bybit_floor=FLOOR,
    )
    real_incident_minimum = 2.66
    assert result.min_binance_usdt > real_incident_minimum + 15.0


def test_31_event_replay_only_intervenes_via_rebalance_first_never_do_not_trade():
    """On this exact real sequence, enough same-exchange inventory had
    already accumulated by the time the floor was ever at risk --
    DO_NOT_TRADE (the more disruptive fallback) should never have been
    necessary here specifically. If this regresses, it's still safe
    (DO_NOT_TRADE never breaches the floor either) but worth noticing:
    it would mean less inventory was available to rebalance with than
    before."""
    result = simulate_event_sequence(
        REAL_EVENTS, starting_binance_usdt=STARTING_BINANCE_USDT, starting_bybit_usdt=STARTING_BYBIT_USDT,
        binance_floor=FLOOR, bybit_floor=FLOOR,
    )
    from app.execution.capital_rebalancer import TradeDecision

    intervened = [s for s in result.steps if s.decision.decision != TradeDecision.ALLOW]
    assert intervened  # at least one intervention happened (redundant with the test above, kept for clarity)
    assert all(s.decision.decision == TradeDecision.REBALANCE_FIRST for s in intervened)
