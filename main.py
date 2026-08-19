"""Entrypoint — boots the FastAPI app together with the collectors, funding
pollers, and the detection/paper-trading loop as background asyncio tasks.
"""

import asyncio
import logging
from contextlib import asynccontextmanager

import uvicorn
from fastapi import FastAPI

from app.api.routes import router
from app.collectors.binance.basis_futures import poll_binance_delivery_futures
from app.collectors.binance.collector import BinanceCollector
from app.collectors.binance.funding import poll_binance_funding
from app.collectors.bybit.collector import BybitCollector
from app.collectors.bybit.funding import poll_bybit_funding
from app.collectors.okx.collector import OkxCollector
from app.collectors.okx.funding import poll_okx_funding
from app.config.constants import (
    CROSS_EXCHANGE_ASSETS,
    DELIVERY_FUTURES_ASSETS,
    MarketType,
    OpportunityClassification,
    PRIORITY_EXCHANGES,
    STABLECOIN_PAIRS,
    TRIANGULAR_CROSS_PAIRS,
)
from app.config.settings import get_settings
from app.database.repository import (
    create_all_tables,
    get_or_create_exchange,
    get_or_create_portfolio,
    save_opportunity,
    save_price_snapshots,
    save_simulated_trade,
)
from app.database.session import async_session_factory
from app.engines.basis import BasisArbitrageEngine
from app.engines.cross_exchange import CrossExchangeArbitrageEngine
from app.engines.funding import FundingArbitrageEngine
from app.engines.stablecoin import StablecoinArbitrageEngine
from app.engines.triangular import TriangularArbitrageEngine
from app.market_data.store import market_data_store
from app.opportunity.detector import OpportunityDetector
from app.simulation.paper_trader import PaperTrader
from app.simulation.portfolios import build_default_portfolios

settings = get_settings()
logging.basicConfig(level=settings.log_level)
logger = logging.getLogger(__name__)

SPOT_SYMBOLS = sorted({f"{a}/USDT" for a in CROSS_EXCHANGE_ASSETS} | set(STABLECOIN_PAIRS) | set(TRIANGULAR_CROSS_PAIRS))
CHART_SYMBOLS = [f"{a}/USDT" for a in CROSS_EXCHANGE_ASSETS]
# Event-driven detection: react to a new tick almost immediately instead of
# polling every few seconds — a real spread observed on 2026-08-19 opened
# and closed within ~15 seconds, which a fixed 3s poll would mostly miss.
# MIN bounds the scan rate during a burst of ticks (protects the DB write
# rate); MAX is the fallback so the loop still ticks even with no new data.
MIN_SCAN_INTERVAL_SECONDS = 0.25
MAX_SCAN_WAIT_SECONDS = 3.0
PAPER_TRADE_CLASSIFICATIONS = {
    OpportunityClassification.INTERESTING,
    OpportunityClassification.GOOD,
    OpportunityClassification.STRONG,
    OpportunityClassification.EXCEPTIONAL,
}

portfolios = build_default_portfolios()
paper_trader = PaperTrader()
background_tasks: list[asyncio.Task] = []


async def detection_loop(detector: OpportunityDetector, portfolio_ids: dict[str, int]) -> None:
    while True:
        try:
            opportunities = await detector.scan_once()
            quotes = [
                q
                for exchange in PRIORITY_EXCHANGES
                for symbol in CHART_SYMBOLS
                if (q := market_data_store.get_quote(exchange, MarketType.SPOT, symbol)) is not None
            ]
            async with async_session_factory() as session:
                if quotes:
                    await save_price_snapshots(session, quotes)
                for opp in opportunities:
                    await save_opportunity(session, opp)
                    if opp.classification in PAPER_TRADE_CLASSIFICATIONS:
                        # Sampled once per opportunity — a maker leg's fill
                        # outcome is a property of the market, not of which
                        # virtual portfolio happens to be replaying it.
                        outcome = paper_trader.determine_outcome(opp)
                        for portfolio in portfolios:
                            trade = paper_trader.simulate(opp, portfolio, outcome)
                            await save_simulated_trade(session, trade, opp.id, portfolio_ids[portfolio.name])
                await session.commit()
            if opportunities:
                logger.info("scan: %d opportunities detected", len(opportunities))
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("detection loop iteration failed")
        await market_data_store.wait_for_update(timeout=MAX_SCAN_WAIT_SECONDS)
        await asyncio.sleep(MIN_SCAN_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await create_all_tables()

    portfolio_ids: dict[str, int] = {}
    async with async_session_factory() as session:
        for exchange in PRIORITY_EXCHANGES:
            await get_or_create_exchange(session, exchange, exchange.capitalize())
        for portfolio in portfolios:
            record = await get_or_create_portfolio(session, portfolio.name, portfolio.initial_capital_usd)
            portfolio_ids[portfolio.name] = record.id
        await session.commit()

    collectors = [
        BinanceCollector(SPOT_SYMBOLS),
        OkxCollector(SPOT_SYMBOLS),
        BybitCollector(SPOT_SYMBOLS),
    ]
    for collector in collectors:
        background_tasks.append(asyncio.create_task(collector.run(market_data_store), name=f"collector:{collector.exchange}"))

    background_tasks.append(asyncio.create_task(poll_binance_funding(market_data_store, CROSS_EXCHANGE_ASSETS), name="funding:binance"))
    background_tasks.append(asyncio.create_task(poll_okx_funding(market_data_store, CROSS_EXCHANGE_ASSETS), name="funding:okx"))
    background_tasks.append(asyncio.create_task(poll_bybit_funding(market_data_store, CROSS_EXCHANGE_ASSETS), name="funding:bybit"))
    background_tasks.append(
        asyncio.create_task(poll_binance_delivery_futures(market_data_store, DELIVERY_FUTURES_ASSETS), name="basis:binance")
    )

    engines = [
        StablecoinArbitrageEngine(),
        CrossExchangeArbitrageEngine(),
        *(TriangularArbitrageEngine(exchange=exchange) for exchange in PRIORITY_EXCHANGES),
        FundingArbitrageEngine(),
        BasisArbitrageEngine(),
    ]
    detector = OpportunityDetector(engines)
    background_tasks.append(asyncio.create_task(detection_loop(detector, portfolio_ids), name="detection_loop"))

    logger.info("startup complete: %d background tasks running", len(background_tasks))
    yield

    for task in background_tasks:
        task.cancel()
    await asyncio.gather(*background_tasks, return_exceptions=True)


app = FastAPI(title=settings.app_name, lifespan=lifespan)
app.include_router(router)


if __name__ == "__main__":
    uvicorn.run(app, host=settings.api_host, port=settings.api_port)
