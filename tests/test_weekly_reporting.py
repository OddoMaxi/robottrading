from app.reporting.weekly import (
    MIN_NET_POSITIVE_RATE_FOR_GO,
    MIN_SAMPLE_SIZE_FOR_VERDICT,
    Verdict,
    _best_exchange_pair,
    determine_verdict,
)


def test_verdict_no_go_with_insufficient_samples_but_no_signal():
    # Below the sample floor should read MODIFY (not enough data), not NO_GO —
    # a strategy shouldn't be killed off before it's had a fair look.
    assert determine_verdict(sample_size=5, net_positive_rate=0.5, avg_net_return_pct=0.2) == Verdict.MODIFY


def test_verdict_go_with_strong_consistent_positive_return():
    assert (
        determine_verdict(
            sample_size=MIN_SAMPLE_SIZE_FOR_VERDICT, net_positive_rate=MIN_NET_POSITIVE_RATE_FOR_GO, avg_net_return_pct=0.15
        )
        == Verdict.GO
    )


def test_verdict_no_go_with_never_profitable():
    assert determine_verdict(sample_size=1000, net_positive_rate=0.0, avg_net_return_pct=-0.2) == Verdict.NO_GO


def test_verdict_modify_when_occasionally_profitable_but_below_go_bar():
    assert determine_verdict(sample_size=1000, net_positive_rate=0.01, avg_net_return_pct=-0.1) == Verdict.MODIFY


def test_best_exchange_pair_picks_highest_average_return():
    rows = [
        ([{"exchange": "binance"}, {"exchange": "okx"}], 0.1),
        ([{"exchange": "binance"}, {"exchange": "okx"}], 0.3),
        ([{"exchange": "binance"}, {"exchange": "bybit"}], 0.05),
    ]
    assert _best_exchange_pair(rows) == "binance / okx"


def test_best_exchange_pair_ignores_single_exchange_legs():
    rows = [([{"exchange": "binance"}, {"exchange": "binance"}], 0.5)]  # triangular, same exchange
    assert _best_exchange_pair(rows) is None


def test_best_exchange_pair_empty_rows():
    assert _best_exchange_pair([]) is None
