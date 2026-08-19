from app.market_data.orderbook import OrderBookLevel, simulate_vwap


def test_vwap_fully_filled_single_level():
    levels = [OrderBookLevel(price=100.0, quantity=10.0)]
    result = simulate_vwap(levels, target_usd=500.0)
    assert result.fully_filled
    assert result.average_price == 100.0
    assert result.filled_usd == 500.0


def test_vwap_walks_multiple_levels():
    levels = [
        OrderBookLevel(price=100.0, quantity=2.0),  # 200 usd
        OrderBookLevel(price=101.0, quantity=2.0),  # 202 usd
    ]
    result = simulate_vwap(levels, target_usd=300.0)
    assert result.fully_filled
    assert result.filled_usd == 300.0
    assert result.worst_price == 101.0
    assert 100.0 < result.average_price < 101.0


def test_vwap_insufficient_depth():
    levels = [OrderBookLevel(price=100.0, quantity=1.0)]
    result = simulate_vwap(levels, target_usd=1000.0)
    assert not result.fully_filled
    assert result.filled_usd == 100.0
