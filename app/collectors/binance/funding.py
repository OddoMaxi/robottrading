"""Binance USDT-M perpetual funding — polled via REST (funding changes slowly; no WS needed)."""

import asyncio
import logging
import time

import aiohttp

from app.market_data.store import FundingSnapshot, MarketDataStore
from app.market_data.symbols import to_native_perp_symbol

logger = logging.getLogger(__name__)

BINANCE_PREMIUM_INDEX_URL = "https://fapi.binance.com/fapi/v1/premiumIndex"


async def poll_binance_funding(
    store: MarketDataStore, assets: list[str], quote_asset: str = "USDT", interval_seconds: float = 30.0
) -> None:
    native_to_asset = {to_native_perp_symbol("binance", f"{a}/{quote_asset}"): a for a in assets}
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(BINANCE_PREMIUM_INDEX_URL, timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    data = await resp.json()
                now = time.time()
                for entry in data:
                    asset = native_to_asset.get(entry.get("symbol"))
                    if asset is None:
                        continue
                    store.update_funding(
                        FundingSnapshot(
                            exchange="binance",
                            symbol=f"{asset}/{quote_asset}",
                            funding_rate=float(entry["lastFundingRate"]),
                            next_funding_time=float(entry["nextFundingTime"]) / 1000,
                            mark_price=float(entry["markPrice"]),
                            index_price=float(entry["indexPrice"]),
                            received_at=now,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("binance funding poll failed")
            await asyncio.sleep(interval_seconds)
