"""Binance quarterly delivery futures — polled via REST (rolls over every ~3
months, no need for a WS subscription). Feeds Engine D (Basis Arbitrage).

Binance-only for now — OKX/Bybit dated futures aren't wired up. Limited to
DELIVERY_FUTURES_ASSETS since most alts on Binance only list a perpetual,
not a dated quarterly contract.
"""

import asyncio
import logging
import time

import aiohttp

from app.market_data.store import DeliveryFuturesSnapshot, MarketDataStore

logger = logging.getLogger(__name__)

BINANCE_EXCHANGE_INFO_URL = "https://fapi.binance.com/fapi/v1/exchangeInfo"
BINANCE_TICKER_PRICE_URL = "https://fapi.binance.com/fapi/v1/ticker/price"


async def _discover_current_quarter_contracts(
    session: aiohttp.ClientSession, assets: list[str], quote_asset: str
) -> dict[str, tuple[str, float]]:
    """Returns {asset: (contract_symbol, delivery_time_epoch_seconds)} for each asset's CURRENT_QUARTER contract."""
    async with session.get(BINANCE_EXCHANGE_INFO_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
        data = await resp.json()

    contracts: dict[str, tuple[str, float]] = {}
    for entry in data.get("symbols", []):
        if entry.get("contractType") != "CURRENT_QUARTER":
            continue
        base_asset = entry.get("baseAsset")
        quote = entry.get("quoteAsset")
        if quote != quote_asset or base_asset not in assets:
            continue
        contracts[base_asset] = (entry["symbol"], float(entry["deliveryDate"]) / 1000)
    return contracts


async def poll_binance_delivery_futures(
    store: MarketDataStore, assets: list[str], quote_asset: str = "USDT", interval_seconds: float = 60.0
) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                contracts = await _discover_current_quarter_contracts(session, assets, quote_asset)
                now = time.time()
                for asset, (contract_symbol, delivery_time) in contracts.items():
                    async with session.get(
                        BINANCE_TICKER_PRICE_URL, params={"symbol": contract_symbol}, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        price_data = await resp.json()
                    store.update_delivery_future(
                        DeliveryFuturesSnapshot(
                            exchange="binance",
                            symbol=f"{asset}/{quote_asset}",
                            contract_symbol=contract_symbol,
                            price=float(price_data["price"]),
                            delivery_time=delivery_time,
                            received_at=now,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("binance delivery futures poll failed")
            await asyncio.sleep(interval_seconds)
