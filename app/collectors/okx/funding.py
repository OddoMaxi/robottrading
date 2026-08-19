"""OKX perpetual (SWAP) funding — polled via REST, one symbol at a time (no bulk endpoint)."""

import asyncio
import logging
import time

import aiohttp

from app.market_data.store import FundingSnapshot, MarketDataStore

logger = logging.getLogger(__name__)

OKX_FUNDING_RATE_URL = "https://www.okx.com/api/v5/public/funding-rate"
OKX_MARK_PRICE_URL = "https://www.okx.com/api/v5/public/mark-price"


async def poll_okx_funding(
    store: MarketDataStore, assets: list[str], quote_asset: str = "USDT", interval_seconds: float = 30.0
) -> None:
    async with aiohttp.ClientSession() as session:
        while True:
            for asset in assets:
                inst_id = f"{asset}-{quote_asset}-SWAP"
                try:
                    async with session.get(
                        OKX_FUNDING_RATE_URL, params={"instId": inst_id}, timeout=aiohttp.ClientTimeout(total=10)
                    ) as resp:
                        funding_data = (await resp.json())["data"][0]

                    async with session.get(
                        OKX_MARK_PRICE_URL,
                        params={"instType": "SWAP", "instId": inst_id},
                        timeout=aiohttp.ClientTimeout(total=10),
                    ) as resp:
                        mark_data = (await resp.json())["data"][0]

                    mark_price = float(mark_data["markPx"])
                    store.update_funding(
                        FundingSnapshot(
                            exchange="okx",
                            symbol=f"{asset}/{quote_asset}",
                            funding_rate=float(funding_data["fundingRate"]),
                            next_funding_time=float(funding_data["nextFundingTime"]) / 1000,
                            mark_price=mark_price,
                            # OKX index price needs a third endpoint call; mark price stands in for it in V1.
                            index_price=mark_price,
                            received_at=time.time(),
                        )
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("okx funding poll failed for %s", inst_id)
            await asyncio.sleep(interval_seconds)
