"""Streamlit dashboard (section 27) — version simplifiée, pensée pour un lecteur non technique.

Run with: streamlit run dashboard/app.py
"""

import asyncio
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# `streamlit run dashboard/app.py` puts dashboard/ on sys.path, not the repo
# root, so the top-level `app` package needs to be added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.constants import CROSS_EXCHANGE_ASSETS, PRIORITY_EXCHANGES
from app.config.settings import get_settings
from app.database.models import OpportunityRecord, PriceSnapshot

st.set_page_config(page_title="Robot d'arbitrage crypto", layout="wide", page_icon="🤖")

STRATEGY_LABELS = {
    "stablecoin": "Stablecoins (USDT/USDC/FDUSD)",
    "cross_exchange": "Entre plateformes (même crypto)",
    "triangular": "Triangulaire (boucle sur 1 plateforme)",
    "funding": "Financement (spot vs futures)",
}

ILLUSTRATIVE_CAPITAL_USD = 1_000


def humanize_delta(detected_at: datetime) -> str:
    if detected_at.tzinfo is None:
        detected_at = detected_at.replace(tzinfo=UTC)
    seconds = max(0, (datetime.now(UTC) - detected_at).total_seconds())
    if seconds < 60:
        return f"il y a {int(seconds)} s"
    if seconds < 3600:
        return f"il y a {int(seconds // 60)} min"
    return f"il y a {int(seconds // 3600)} h"


async def fetch_opportunities(limit: int = 300) -> pd.DataFrame:
    # Streamlit calls asyncio.run() fresh on every rerun, giving each run its
    # own event loop. asyncpg connections can't be reused across event loops,
    # so — unlike the FastAPI app's long-lived engine — this one is created
    # and disposed within the same run rather than shared at module level.
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(OpportunityRecord).order_by(OpportunityRecord.detected_at.desc()).limit(limit)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    return pd.DataFrame(
        [
            {
                "Stratégie": STRATEGY_LABELS.get(r.strategy, r.strategy),
                "Paire": r.symbol,
                "Gain brut (%)": float(r.gross_spread_pct),
                "Gain net (%)": float(r.net_spread_pct) if r.net_spread_pct is not None else None,
                "Résultat sur 1000 $": (float(r.net_spread_pct) / 100 * ILLUSTRATIVE_CAPITAL_USD)
                if r.net_spread_pct is not None
                else None,
                "Détecté": humanize_delta(r.detected_at),
                "_detected_at": r.detected_at,
            }
            for r in rows
        ]
    )


async def fetch_price_history(symbol: str, hours: float = 3.0) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        cutoff = datetime.now(UTC) - timedelta(hours=hours)
        async with session_factory() as session:
            result = await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.symbol == symbol, PriceSnapshot.recorded_at >= cutoff)
                .order_by(PriceSnapshot.recorded_at)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    return pd.DataFrame(
        [
            {
                "exchange": r.exchange,
                "recorded_at": r.recorded_at,
                "mid": (float(r.bid) + float(r.ask)) / 2,
            }
            for r in rows
        ]
    )


df = asyncio.run(fetch_opportunities())
profitable = df[df["Gain net (%)"] > 0] if not df.empty else df

st.title("🤖 Robot d'arbitrage crypto")
st.caption("Le robot compare en continu les prix sur Binance, OKX et Bybit — aucun argent réel n'est engagé (simulation uniquement).")

# --- Bandeau de statut, en langage simple ---
if df.empty:
    st.info("Le robot vient de démarrer, pas encore de données à afficher. Reviens dans quelques instants.")
elif not profitable.empty:
    best = profitable.sort_values("Résultat sur 1000 $", ascending=False).iloc[0]
    st.success(
        f"✅ {len(profitable)} opportunité(s) actuellement rentable(s) après frais. "
        f"La meilleure : **{best['Paire']}** ({best['Stratégie']}) — "
        f"environ **{best['Résultat sur 1000 $']:+.2f} $** sur 1000 $ investis."
    )
else:
    st.warning(
        "❌ Aucune opportunité rentable pour le moment — les écarts de prix repérés sont trop petits "
        "pour couvrir les frais des plateformes. C'est normal et fréquent : le robot continue de "
        "chercher 24h/24, il suffit qu'un écart plus grand apparaisse."
    )

# --- Chiffres clés ---
col1, col2, col3 = st.columns(3)
col1.metric("Opportunités repérées (récentes)", len(df))
col2.metric("Dont rentables après frais", len(profitable) if not df.empty else 0)
col3.metric(
    "Meilleur résultat sur 1000 $",
    f"{df['Résultat sur 1000 $'].max():+.2f} $" if not df.empty else "—",
)

st.divider()

# --- Graphiques de prix ---
st.subheader("📈 Graphique des prix")

chart_symbols = [f"{a}/USDT" for a in CROSS_EXCHANGE_ASSETS]
chart_col1, chart_col2 = st.columns([2, 1])
chart_symbol = chart_col1.selectbox("Paire", chart_symbols)
chart_timeframe = chart_col2.selectbox("Période", ["1 min", "5 min", "15 min"], index=0)
resample_rule = {"1 min": "1min", "5 min": "5min", "15 min": "15min"}[chart_timeframe]

price_df = asyncio.run(fetch_price_history(chart_symbol))

if price_df.empty:
    st.info(
        "Pas encore assez d'historique pour tracer un graphique — le robot vient de démarrer la "
        "collecte des prix. Reviens dans quelques minutes."
    )
else:
    exchanges_present = sorted(price_df["exchange"].unique())

    candle_exchange = st.selectbox(
        "Bougies (chandelier japonais) sur quelle plateforme ?",
        [e.capitalize() for e in exchanges_present],
    )
    candles = (
        price_df[price_df["exchange"] == candle_exchange.lower()]
        .set_index("recorded_at")["mid"]
        .resample(resample_rule)
        .ohlc()
        .dropna()
    )

    if len(candles) < 2:
        st.info("Pas encore assez de points sur cette période pour former des bougies — patiente un peu ou choisis une période plus courte.")
    else:
        candle_fig = go.Figure(
            data=[
                go.Candlestick(
                    x=candles.index,
                    open=candles["open"],
                    high=candles["high"],
                    low=candles["low"],
                    close=candles["close"],
                    increasing_line_color="#2ecc71",
                    decreasing_line_color="#e74c3c",
                )
            ]
        )
        candle_fig.update_layout(
            title=f"{chart_symbol} — {candle_exchange}",
            xaxis_rangeslider_visible=False,
            height=420,
            margin=dict(l=10, r=10, t=40, b=10),
        )
        st.plotly_chart(candle_fig, use_container_width=True)

    st.caption("Comparaison du prix sur les 3 plateformes — un écart visible entre les lignes, c'est une opportunité d'arbitrage.")
    compare_fig = go.Figure()
    for exchange in exchanges_present:
        sub = price_df[price_df["exchange"] == exchange]
        compare_fig.add_trace(go.Scatter(x=sub["recorded_at"], y=sub["mid"], mode="lines", name=exchange.capitalize()))
    compare_fig.update_layout(height=320, margin=dict(l=10, r=10, t=10, b=10))
    st.plotly_chart(compare_fig, use_container_width=True)

st.divider()

# --- Tableau principal, simplifié ---
st.subheader("Dernières opportunités repérées")

with st.expander("Comment lire ce tableau ?"):
    st.markdown(
        """
        - **Gain brut** : l'écart de prix repéré entre deux plateformes (ou dans une boucle), avant les frais.
        - **Gain net** : ce qu'il resterait *après* avoir payé les frais des plateformes — c'est le seul chiffre qui compte vraiment.
        - **Résultat sur 1000 $** : à titre d'exemple, ce que ça donnerait en dollars si on investissait 1000 $ dans cette opportunité (négatif = perte après frais).
        - Le robot ne fait **aucune opération réelle** pour l'instant : tout est simulé pour évaluer si la stratégie vaut le coup avant d'y mettre du vrai capital.
        """
    )

if df.empty:
    st.info("Rien à afficher pour l'instant.")
else:
    display_df = df.sort_values("_detected_at", ascending=False).drop(columns=["_detected_at"]).head(50)

    def highlight_profit(row: pd.Series) -> list[str]:
        net = row["Gain net (%)"]
        if pd.isna(net):
            color = "background-color: #333333"
        elif net > 0:
            color = "background-color: #1e4620"
        else:
            color = "background-color: #4a1e1e"
        return [color] * len(row)

    st.dataframe(
        display_df.style.apply(highlight_profit, axis=1).format(
            {"Gain brut (%)": "{:.3f} %", "Gain net (%)": "{:.3f} %", "Résultat sur 1000 $": "{:+.2f} $"}
        ),
        use_container_width=True,
        hide_index=True,
    )

st.divider()

# --- Répartition simple par stratégie ---
st.subheader("Combien d'opportunités par stratégie ?")
if df.empty:
    st.info("Rien à comparer pour l'instant.")
else:
    st.bar_chart(df["Stratégie"].value_counts())

st.caption(f"Plateformes surveillées : {', '.join(e.capitalize() for e in PRIORITY_EXCHANGES)}")
