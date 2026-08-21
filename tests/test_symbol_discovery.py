from app.market_data.symbol_discovery import DiscoveryResult, build_discovered_universe


def test_asset_listed_on_2_exchanges_is_included():
    results = [
        DiscoveryResult("binance", reachable=True, quote_volume_by_symbol={"BTC/USDT": 10_000_000, "SOL/USDT": 1_000_000}),
        DiscoveryResult("okx", reachable=True, quote_volume_by_symbol={"BTC/USDT": 5_000_000}),
        DiscoveryResult("bybit", reachable=True, quote_volume_by_symbol={}),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000)
    assert "BTC" in universe.assets_on_2_or_more_exchanges
    assert "SOL" not in universe.assets_on_2_or_more_exchanges  # only listed on 1 exchange


def test_below_volume_floor_is_excluded_even_if_listed_everywhere():
    results = [
        DiscoveryResult("binance", reachable=True, quote_volume_by_symbol={"SHIB/USDT": 100_000}),
        DiscoveryResult("okx", reachable=True, quote_volume_by_symbol={"SHIB/USDT": 100_000}),
        DiscoveryResult("bybit", reachable=True, quote_volume_by_symbol={"SHIB/USDT": 100_000}),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000)
    assert universe.assets_on_2_or_more_exchanges == []


def test_ranked_by_the_weakest_leg_volume_not_the_strongest():
    results = [
        DiscoveryResult("binance", reachable=True, quote_volume_by_symbol={"A/USDT": 50_000_000, "B/USDT": 2_000_000}),
        DiscoveryResult("okx", reachable=True, quote_volume_by_symbol={"A/USDT": 600_000, "B/USDT": 1_800_000}),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000, max_assets=10)
    # A's cross-exchange floor is 600k (its OKX leg), B's is 1.8M (its OKX
    # leg) — B should rank ahead of A despite A's much bigger Binance number.
    assert universe.assets_on_2_or_more_exchanges == ["B", "A"] or universe.assets_on_2_or_more_exchanges[0] == "B"


def test_max_assets_caps_the_result():
    results = [
        DiscoveryResult(
            "binance",
            reachable=True,
            quote_volume_by_symbol={f"ASSET{i}/USDT": 1_000_000 - i for i in range(20)},
        ),
        DiscoveryResult(
            "okx",
            reachable=True,
            quote_volume_by_symbol={f"ASSET{i}/USDT": 1_000_000 - i for i in range(20)},
        ),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000, max_assets=5)
    assert len(universe.assets_on_2_or_more_exchanges) == 5


def test_unreachable_exchange_sets_degraded_flag():
    results = [
        DiscoveryResult("binance", reachable=True, quote_volume_by_symbol={"BTC/USDT": 10_000_000}),
        DiscoveryResult("okx", reachable=False),
        DiscoveryResult("bybit", reachable=True, quote_volume_by_symbol={"BTC/USDT": 10_000_000}),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000)
    assert universe.degraded is True


def test_all_reachable_is_not_degraded():
    results = [
        DiscoveryResult("binance", reachable=True, quote_volume_by_symbol={}),
        DiscoveryResult("okx", reachable=True, quote_volume_by_symbol={}),
        DiscoveryResult("bybit", reachable=True, quote_volume_by_symbol={}),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000)
    assert universe.degraded is False


def test_listed_on_tracks_which_exchanges_confirmed_each_symbol():
    results = [
        DiscoveryResult("binance", reachable=True, quote_volume_by_symbol={"BTC/USDT": 10_000_000}),
        DiscoveryResult("okx", reachable=True, quote_volume_by_symbol={"BTC/USDT": 10_000_000}),
        DiscoveryResult("bybit", reachable=True, quote_volume_by_symbol={}),
    ]
    universe = build_discovered_universe(results, min_quote_volume_usd=500_000)
    assert universe.listed_on["BTC/USDT"] == {"binance", "okx"}
    assert "BTC/USDT" not in universe.listed_on.get("bybit", set())
