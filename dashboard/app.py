"""Streamlit dashboard (section 27) — V1 quick dashboard, reads straight from Postgres.

Run with: streamlit run dashboard/app.py
Shows zeros/placeholders until the Market Data Engine and arbitrage engines
are actually producing opportunities.
"""

import asyncio

import pandas as pd
import streamlit as st
from sqlalchemy import select

from app.config.constants import PRIORITY_EXCHANGES
from app.database.models import OpportunityRecord
from app.database.session import async_session_factory

st.set_page_config(page_title="Multi-Market Arbitrage Engine", layout="wide")


async def fetch_opportunities(limit: int = 200) -> pd.DataFrame:
    async with async_session_factory() as session:
        result = await session.execute(
            select(OpportunityRecord).order_by(OpportunityRecord.detected_at.desc()).limit(limit)
        )
        rows = result.scalars().all()
    return pd.DataFrame(
        [
            {
                "strategy": r.strategy,
                "symbol": r.symbol,
                "gross_spread_pct": r.gross_spread_pct,
                "net_spread_pct": r.net_spread_pct,
                "score": r.score,
                "classification": r.classification,
                "detected_at": r.detected_at,
            }
            for r in rows
        ]
    )


df = asyncio.run(fetch_opportunities())

st.title("MULTI-MARKET ARBITRAGE ENGINE")
st.caption("LIVE — Market Observation + Paper Trading (V1)")

col1, col2, col3, col4 = st.columns(4)
col1.metric("Exchanges Online", f"?/{len(PRIORITY_EXCHANGES)}")
col2.metric("Opportunities Today", len(df))
col3.metric("Net Opportunities", int((df["net_spread_pct"] > 0).sum()) if not df.empty else 0)
col4.metric("Best Opportunity", f"{df['net_spread_pct'].max():.2f} %" if not df.empty else "—")

st.subheader("Live Opportunities")
if df.empty:
    st.info("No opportunities recorded yet — collectors and engines are not wired up.")
else:
    st.dataframe(df, use_container_width=True)

st.subheader("Strategy Comparison")
if df.empty:
    st.info("Nothing to compare yet.")
else:
    st.dataframe(
        df.groupby("strategy")["net_spread_pct"].agg(["count", "mean", "max"]),
        use_container_width=True,
    )
