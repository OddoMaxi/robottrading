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
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

# `streamlit run dashboard/app.py` puts dashboard/ on sys.path, not the repo
# root, so the top-level `app` package needs to be added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analytics.fees import FeeEngine
from app.analytics.maker_simulation import MakerAssumptions, best_maker_pair
from app.config.constants import CROSS_EXCHANGE_ASSETS, PRIORITY_EXCHANGES
from app.config.fees import DEFAULT_FEE_SCHEDULES, uniform_fee_schedules
from app.config.settings import get_settings
from app.database.models import OpportunityRecord, PriceSnapshot
from app.reporting.daily import DailySummary, build_daily_summary
from app.reporting.weekly import Verdict, WeeklyAnalytics, build_weekly_analytics

st.set_page_config(page_title="Robot d'arbitrage crypto", layout="wide", page_icon="🤖")

STRATEGY_LABELS = {
    "stablecoin": "Stablecoins (USDT/USDC/FDUSD)",
    "cross_exchange": "Entre plateformes (même crypto)",
    "triangular": "Triangulaire (boucle sur 1 plateforme)",
    "funding": "Financement (spot vs perpetual)",
    "basis": "Basis (spot vs future à échéance)",
}

EXECUTION_MODE_LABELS = {
    "taker_taker": "Marché / Marché",
    "maker_taker": "Limite / Marché",
    "taker_maker": "Marché / Limite",
    "maker_maker": "Limite / Limite",
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
                "Seuil de rentabilité (%)": float(r.break_even_pct) if r.break_even_pct is not None else None,
                "Gain net (%)": float(r.net_spread_pct) if r.net_spread_pct is not None else None,
                "Résultat sur 1000 $": (float(r.net_spread_pct) / 100 * ILLUSTRATIVE_CAPITAL_USD)
                if r.net_spread_pct is not None
                else None,
                "Meilleure exécution": EXECUTION_MODE_LABELS.get(r.execution_mode, "—"),
                "Proba. exécution": float(r.execution_fill_probability) * 100 if r.execution_fill_probability is not None else None,
                "Détecté": humanize_delta(r.detected_at),
                "_detected_at": r.detected_at,
            }
            for r in rows
        ]
    )


async def fetch_last_profitable_spike() -> dict | None:
    """Most recent opportunity that was genuinely profitable after fees, plus how often that's happened lately.

    Queried directly (not from the already-fetched recent-opportunities
    table) because that table is capped at a few hundred rows and, since
    detection went event-driven, that can now be just the last few seconds
    — too short a window to find a rare profitable spike in.
    """
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(OpportunityRecord)
                .where(OpportunityRecord.net_spread_pct > 0)
                .order_by(OpportunityRecord.detected_at.desc())
                .limit(1)
            )
            row = result.scalar_one_or_none()
            if row is None:
                return None

            cutoff = (datetime.now(UTC) - timedelta(hours=24)).replace(tzinfo=None)
            count_result = await session.execute(
                select(func.count()).where(OpportunityRecord.net_spread_pct > 0, OpportunityRecord.detected_at >= cutoff)
            )
            count_24h = count_result.scalar()
    finally:
        await engine.dispose()
    return {
        "symbol": row.symbol,
        "strategy": STRATEGY_LABELS.get(row.strategy, row.strategy),
        "net_spread_pct": float(row.net_spread_pct),
        "detected_at": row.detected_at,
        "count_24h": count_24h,
    }


@st.cache_data(ttl=15)
def get_last_profitable_spike_cached() -> dict | None:
    return asyncio.run(fetch_last_profitable_spike())


async def fetch_price_history(symbol: str, hours: float = 3.0) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        # recorded_at is stored as a naive UTC timestamp (server_default=func.now(),
        # no timezone=True) — strip tzinfo so asyncpg isn't asked to compare
        # a naive column against an aware bind parameter.
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
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


async def fetch_bid_ask_history(symbols: list[str], hours: float = 2.0) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
        async with session_factory() as session:
            result = await session.execute(
                select(PriceSnapshot).where(PriceSnapshot.symbol.in_(symbols), PriceSnapshot.recorded_at >= cutoff)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    return pd.DataFrame(
        [
            {"symbol": r.symbol, "exchange": r.exchange, "recorded_at": r.recorded_at, "bid": float(r.bid), "ask": float(r.ask)}
            for r in rows
        ]
    )


@st.cache_data(ttl=30)
def get_bid_ask_history_cached(symbols: tuple[str, ...], hours: float) -> pd.DataFrame:
    return asyncio.run(fetch_bid_ask_history(list(symbols), hours))


def compute_maker_analysis(
    raw_df: pd.DataFrame, assumptions: MakerAssumptions, capital_usd: float, fee_engine: FeeEngine, resample_rule: str = "1min"
) -> pd.DataFrame:
    """Replay real observed bid/ask history through the maker-order what-if model."""
    rows = []
    for symbol, group in raw_df.groupby("symbol"):
        bids = group.pivot_table(index="recorded_at", columns="exchange", values="bid").resample(resample_rule).last().ffill(limit=2)
        asks = group.pivot_table(index="recorded_at", columns="exchange", values="ask").resample(resample_rule).last().ffill(limit=2)
        for ts in bids.index:
            bid_row, ask_row = bids.loc[ts], asks.loc[ts]
            quotes = {
                exchange: (bid_row[exchange], ask_row[exchange])
                for exchange in bid_row.index
                if pd.notna(bid_row[exchange]) and pd.notna(ask_row[exchange])
            }
            if len(quotes) < 2:
                continue
            best = best_maker_pair(quotes, capital_usd, fee_engine, assumptions)
            if best is None:
                continue
            buy_exchange, sell_exchange, result = best
            rows.append(
                {
                    "Paire": symbol,
                    "Achat sur": buy_exchange.capitalize(),
                    "Vente sur": sell_exchange.capitalize(),
                    "Valeur espérée (%)": result.expected_value_pct,
                    "Valeur espérée sur 1000 $": result.expected_value_usd / capital_usd * ILLUSTRATIVE_CAPITAL_USD,
                    "recorded_at": ts,
                }
            )
    return pd.DataFrame(rows)


async def fetch_daily_summary() -> DailySummary:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_daily_summary(session)
    finally:
        await engine.dispose()


async def fetch_weekly_analytics() -> WeeklyAnalytics:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            return await build_weekly_analytics(session)
    finally:
        await engine.dispose()


@st.cache_data(ttl=60)
def get_daily_summary_cached() -> DailySummary:
    return asyncio.run(fetch_daily_summary())


@st.cache_data(ttl=300)
def get_weekly_analytics_cached() -> WeeklyAnalytics:
    return asyncio.run(fetch_weekly_analytics())


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

# --- Dernier pic rentable (historique, pas l'instant présent) ---
last_spike = get_last_profitable_spike_cached()
if last_spike is None:
    st.info("🎯 Aucun pic rentable détecté depuis le début de l'observation — c'est rare, pas un problème.")
else:
    st.info(
        f"🎯 Dernier pic rentable détecté : **{humanize_delta(last_spike['detected_at'])}** "
        f"({last_spike['detected_at'].strftime('%d/%m %H:%M:%S')} UTC) — "
        f"**{last_spike['symbol']}** ({last_spike['strategy']}), "
        f"**+{last_spike['net_spread_pct']:.2f}% net**. "
        f"{last_spike['count_24h']} pic(s) rentable(s) sur les dernières 24h."
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

# --- Simulation "et si on utilisait des ordres maker ?" ---
st.subheader("🧪 Et si on utilisait des ordres maker au lieu du marché ?")
st.caption(
    "Les ordres maker (limite) coûtent moins cher en frais, mais rien ne garantit qu'ils se remplissent "
    "à temps — c'est une hypothèse à tester, pas une mesure réelle."
)

with st.expander("Comment ça marche ?", expanded=False):
    st.markdown(
        """
        - Un ordre **marché (taker)** s'exécute tout de suite, mais coûte plus cher en frais.
        - Un ordre **limite (maker)** coûte moins cher, mais reste en attente — s'il ne se remplit pas
          avant que le prix bouge, l'opportunité peut disparaître, ou pire, un seul des deux côtés se
          remplit et on se retrouve exposé sans protection (il faut alors sortir en urgence, à perte).
        - **On n'a aucune donnée réelle** sur la fréquence à laquelle ces ordres se remplissent sur ces
          plateformes — les deux premiers curseurs sont donc des hypothèses à ajuster, pas des faits.
          Un vrai test (compte de démo / testnet) serait nécessaire pour les remplacer par des chiffres mesurés.
        """
    )

maker_col1, maker_col2, maker_col3 = st.columns(3)
fill_probability_pct = maker_col1.slider("Chance qu'un ordre se remplisse à temps", 10, 95, 55, help="Hypothèse — pas une mesure.")
adverse_move_pct = maker_col2.slider("Perte si un seul côté se remplit (%)", 0.01, 0.30, 0.05, step=0.01)
maker_hours = maker_col3.selectbox("Sur quelle période ?", [1.0, 2.0, 6.0], index=1, format_func=lambda h: f"Dernières {int(h)} h")

st.caption(
    "« Et si j'avais un statut VIP ? » — règle tes frais réels ci-dessous (visibles dans les paramètres "
    "de ton compte sur chaque exchange). Les tarifs VIP publiés changent souvent et diffèrent par "
    "plateforme, donc on ne devine pas de palier ici : mets le chiffre exact que ton compte affiche."
)
fee_col1, fee_col2 = st.columns(2)
default_taker_pct = DEFAULT_FEE_SCHEDULES["binance"].taker_fee_spot * 100
default_maker_pct = DEFAULT_FEE_SCHEDULES["binance"].maker_fee_spot * 100
taker_fee_pct = fee_col1.slider("Frais marché — taker (%)", 0.00, 0.10, float(default_taker_pct), step=0.01)
maker_fee_pct = fee_col2.slider("Frais limite — maker (%)", 0.00, 0.10, float(default_maker_pct), step=0.01)

maker_raw_df = get_bid_ask_history_cached(tuple(chart_symbols), maker_hours)

if maker_raw_df.empty:
    st.info("Pas encore assez d'historique pour cette simulation.")
else:
    assumptions = MakerAssumptions(fill_probability=fill_probability_pct / 100, adverse_move_pct=adverse_move_pct)
    fee_engine = FeeEngine(uniform_fee_schedules(maker_fee_pct / 100, taker_fee_pct / 100))
    maker_results = compute_maker_analysis(maker_raw_df, assumptions, ILLUSTRATIVE_CAPITAL_USD, fee_engine)

    if maker_results.empty:
        st.info("Pas assez de données communes entre plateformes pour cette période.")
    else:
        positive_share = (maker_results["Valeur espérée (%)"] > 0).mean() * 100
        avg_ev = maker_results["Valeur espérée sur 1000 $"].mean()

        verdict_col1, verdict_col2 = st.columns(2)
        verdict_col1.metric("Instants où c'était rentable (en espérance)", f"{positive_share:.1f} %")
        verdict_col2.metric("Résultat moyen sur 1000 $ (en espérance)", f"{avg_ev:+.2f} $")

        if positive_share > 50:
            st.success("Sous ces hypothèses, les ordres maker rendraient cette stratégie rentable plus souvent qu'improbable.")
        else:
            st.warning(
                "Sous ces hypothèses, même avec des frais réduits, le risque de non-remplissage mange "
                "l'avantage la plupart du temps — essaie d'augmenter la chance de remplissage pour voir "
                "à partir de quel niveau ça basculerait."
            )

        st.caption("Meilleures paires sous ces hypothèses (valeur espérée moyenne) :")
        top_pairs = (
            maker_results.groupby("Paire")["Valeur espérée sur 1000 $"]
            .mean()
            .sort_values(ascending=False)
            .head(10)
        )
        st.bar_chart(top_pairs)

st.divider()

# --- Tableau principal, simplifié ---
st.subheader("Dernières opportunités repérées")

with st.expander("Comment lire ce tableau ?"):
    st.markdown(
        """
        - **Gain brut** : l'écart de prix repéré entre deux plateformes (ou dans une boucle), avant les frais.
        - **Seuil de rentabilité** : l'écart minimum nécessaire pour couvrir les frais + une marge de sécurité. Si le gain brut est en dessous, ce n'est même pas la peine de calculer le reste — ça ne sera jamais rentable.
        - **Gain net** : ce qu'il resterait *après* avoir payé les frais des plateformes — c'est le seul chiffre qui compte vraiment.
        - **Résultat sur 1000 $** : à titre d'exemple, ce que ça donnerait en dollars si on investissait 1000 $ dans cette opportunité (négatif = perte après frais).
        - **Meilleure exécution** : parmi les 4 façons de passer les deux ordres (marché ou limite, sur chaque côté), celle qui rapporte le plus *en moyenne pondérée par le risque de non-remplissage*. « Limite » coûte moins cher en frais mais peut ne pas s'exécuter à temps.
        - **Proba. exécution** : estimation (pas une mesure réelle) de la probabilité que cette méthode s'exécute effectivement, basée sur l'écart bid/ask, la liquidité disponible et la volatilité récente.
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
            {
                "Gain brut (%)": "{:.3f} %",
                "Seuil de rentabilité (%)": "{:.3f} %",
                "Gain net (%)": "{:.3f} %",
                "Résultat sur 1000 $": "{:+.2f} $",
                "Proba. exécution": "{:.0f} %",
            }
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

st.divider()

# --- Rapport quotidien ---
st.subheader("📅 Résumé du jour (24 dernières heures)")
daily = get_daily_summary_cached()

day_col1, day_col2, day_col3, day_col4 = st.columns(4)
day_col1.metric("Opportunités repérées", daily.detected)
day_col2.metric("Dont rentables (net > 0)", daily.net_positive)
day_col3.metric("Trades simulés", daily.paper_trades)
day_col4.metric("P&L simulé cumulé", f"{daily.simulated_net_pnl_usd:+.2f} $")

if daily.best_strategy:
    st.caption(
        f"Meilleure stratégie sur 24h : **{STRATEGY_LABELS.get(daily.best_strategy, daily.best_strategy)}** — "
        f"pire : **{STRATEGY_LABELS.get(daily.worst_strategy, daily.worst_strategy)}** — "
        f"meilleur actif : **{daily.best_asset or '—'}**"
    )

st.divider()

# --- Rapport hebdomadaire + verdict GO/MODIFY/NO-GO ---
st.subheader("📊 Bilan sur 7 jours — quelle stratégie garder ?")
weekly = get_weekly_analytics_cached()

week_col1, week_col2, week_col3, week_col4 = st.columns(4)
week_col1.metric("Opportunités (7j)", weekly.total_opportunities)
week_col2.metric("Trades exécutés (simulés)", weekly.executed_simulations)
week_col3.metric("Occasions manquées", weekly.missed_opportunities)
week_col4.metric("P&L simulé (7j)", f"{weekly.net_simulated_pnl_usd:+.2f} $")

context_bits = []
if weekly.best_asset:
    context_bits.append(f"meilleur actif : **{weekly.best_asset}**")
if weekly.best_exchange_pair:
    context_bits.append(f"meilleure paire d'exchanges : **{weekly.best_exchange_pair}**")
if weekly.best_trading_hour_utc is not None:
    context_bits.append(f"meilleure heure (UTC) : **{weekly.best_trading_hour_utc}h**")
if context_bits:
    st.caption(" — ".join(context_bits))

with st.expander("Comment lire le verdict par stratégie ?"):
    st.markdown(
        """
        - 🟢 **GO** : rentable de façon consistante (≥ 5 % des occasions positives en net, en moyenne positif) sur au moins 30 observations.
        - 🟠 **MODIFY** : soit pas encore assez de données (< 30 observations), soit occasionnellement rentable mais pas assez consistant.
        - 🔴 **NO-GO** : jamais rentable après frais sur la période observée.
        - Ces seuils sont des points de départ — à recalibrer après une vraie semaine d'observation continue.
        """
    )

if not weekly.by_strategy:
    st.info("Pas encore assez de données sur 7 jours pour un verdict.")
else:
    verdict_badges = {Verdict.GO: "🟢 GO", Verdict.MODIFY: "🟠 MODIFY", Verdict.NO_GO: "🔴 NO-GO"}
    verdict_df = pd.DataFrame(
        [
            {
                "Stratégie": STRATEGY_LABELS.get(s.strategy, s.strategy),
                "Opportunités": s.total_opportunities,
                "Rentables (%)": s.net_positive_rate * 100,
                "Rendement net moyen (%)": s.avg_net_return_pct,
                "Rendement net médian (%)": s.median_net_return_pct,
                "Verdict": verdict_badges[s.verdict],
            }
            for s in sorted(weekly.by_strategy, key=lambda s: s.avg_net_return_pct, reverse=True)
        ]
    )
    st.dataframe(
        verdict_df.style.format({"Rentables (%)": "{:.2f} %", "Rendement net moyen (%)": "{:.4f} %", "Rendement net médian (%)": "{:.4f} %"}),
        use_container_width=True,
        hide_index=True,
    )

st.caption(f"Plateformes surveillées : {', '.join(e.capitalize() for e in PRIORITY_EXCHANGES)}")
