"""Streamlit dashboard (section 27) — version simplifiée, pensée pour un lecteur non technique.

Run with: streamlit run dashboard/app.py
"""

import asyncio
import sys
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# `streamlit run dashboard/app.py` puts dashboard/ on sys.path, not the repo
# root, so the top-level `app` package needs to be added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.config.constants import PRIORITY_EXCHANGES
from app.config.settings import get_settings
from app.database.models import OpportunityRecord

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
