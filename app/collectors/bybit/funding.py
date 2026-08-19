"""Bybit linear-perpetual funding — polled via REST, one bulk call for all symbols."""

import asyncio
import logging
import time

import aiohttp

from app.market_data.store import FundingSnapshot, MarketDataStore

logger = logging.getLogger(__name__)

BYBIT_TICKERS_URL = "https://api.bybit.com/v5/market/tickers"


async def poll_bybit_funding(
    store: MarketDataStore, assets: list[str], quote_asset: str = "USDT", interval_seconds: float = 30.0
) -> None:
    native_to_asset = {f"{a}{quote_asset}": a for a in assets}
    async with aiohttp.ClientSession() as session:
        while True:
            try:
                async with session.get(
                    BYBIT_TICKERS_URL, params={"category": "linear"}, timeout=aiohttp.ClientTimeout(total=10)
                ) as resp:
                    payload = await resp.json()
                now = time.time()
                for entry in payload.get("result", {}).get("list", []):
                    asset = native_to_asset.get(entry.get("symbol"))
                    if asset is None or not entry.get("fundingRate"):
                        continue
                    store.update_funding(
                        FundingSnapshot(
                            exchange="bybit",
                            symbol=f"{asset}/{quote_asset}",
                            funding_rate=float(entry["fundingRate"]),
                            next_funding_time=float(entry["nextFundingTime"]) / 1000,
                            mark_price=float(entry["markPrice"]),
                            index_price=float(entry["indexPrice"]),
                            received_at=now,
                        )
                    )
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("bybit funding poll failed")
            await asyncio.sleep(interval_seconds)
