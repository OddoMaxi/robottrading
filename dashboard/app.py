"""Streamlit dashboard (section 27) — dark, card-based design, pensé pour un lecteur non technique.

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
from app.database.models import OpportunityRecord, PriceSnapshot, VirtualPortfolioRecord
from app.reporting.daily import DailySummary, build_daily_summary
from app.reporting.holding_time_performance import HoldingTimeBucketStats, build_holding_time_performance
from app.reporting.rotation import RotationReport, build_rotation_report
from app.reporting.weekly import Verdict, WeeklyAnalytics, build_weekly_analytics

st.set_page_config(page_title="Robot d'arbitrage crypto", layout="wide", page_icon="🤖")

# --- Design tokens — validated dark palette (dataviz skill: references/palette.md) ---
SURFACE = "#1a1a19"
PAGE = "#0d0d0d"
INK_PRIMARY = "#ffffff"
INK_SECONDARY = "#c3c2b7"
INK_MUTED = "#898781"
GRIDLINE = "#2c2c2a"
BASELINE = "#383835"
BORDER = "rgba(255,255,255,0.10)"
FONT_STACK = "system-ui, -apple-system, 'Segoe UI', sans-serif"

SEQUENTIAL_BLUE = "#3987e5"

# Categorical, fixed order — never re-mapped, so an exchange keeps its color everywhere.
EXCHANGE_COLORS = {"binance": "#3987e5", "okx": "#d95926", "bybit": "#199e70"}

STATUS_GOOD = "#0ca30c"
STATUS_WARNING = "#fab219"
STATUS_SERIOUS = "#ec835a"
STATUS_CRITICAL = "#d03b3b"

STRATEGY_LABELS = {
    "stablecoin": "Stablecoins (USDT/USDC/FDUSD)",
    "cross_exchange": "Entre plateformes (même crypto)",
    "triangular": "Triangulaire (boucle sur 1 plateforme)",
    "funding": "Financement (spot vs perpetual)",
    "basis": "Basis (spot vs future à échéance)",
}

HOLDING_TIME_LABELS = {
    "ultra_fast": "Ultra Fast (< 30 s)",
    "fast": "Fast (< 5 min)",
    "medium": "Medium (< 30 min)",
    "carry": "Carry (≥ 30 min)",
}

EXECUTION_MODE_LABELS = {
    "taker_taker": "Marché / Marché",
    "maker_taker": "Limite / Marché",
    "taker_maker": "Marché / Limite",
    "maker_maker": "Limite / Limite",
}

ILLUSTRATIVE_CAPITAL_USD = 1_000

st.markdown(
    f"""
    <style>
    html, body, [class*="css"] {{ font-family: {FONT_STACK}; }}

    /* Tighten Streamlit's default top padding for a denser, dashboard feel */
    .block-container {{ padding-top: 2rem; padding-bottom: 3rem; max-width: 1200px; }}

    h1, h2, h3 {{ font-weight: 600 !important; letter-spacing: -0.01em; }}

    hr {{ border-color: {GRIDLINE} !important; margin: 2rem 0 !important; }}

    /* --- Stat card grid --- */
    .stat-grid {{
        display: grid;
        grid-template-columns: repeat(auto-fit, minmax(190px, 1fr));
        gap: 12px;
        margin: 4px 0 8px 0;
    }}
    .stat-card {{
        background: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: 14px;
        padding: 18px 20px;
    }}
    .stat-label {{
        color: {INK_MUTED};
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.05em;
        margin-bottom: 8px;
    }}
    .stat-value {{
        color: {INK_PRIMARY};
        font-size: 1.85rem;
        font-weight: 600;
        line-height: 1.1;
    }}
    .stat-sub {{
        color: {INK_SECONDARY};
        font-size: 0.82rem;
        margin-top: 6px;
    }}
    .delta-good {{ color: {STATUS_GOOD}; font-weight: 600; }}
    .delta-bad {{ color: {STATUS_CRITICAL}; font-weight: 600; }}

    /* --- Hero status banner --- */
    .hero-card {{
        border-radius: 16px;
        padding: 24px 28px;
        margin: 8px 0 20px 0;
        border: 1px solid {BORDER};
    }}
    .hero-card.hero-good {{ background: rgba(12,163,12,0.10); border-color: rgba(12,163,12,0.35); }}
    .hero-card.hero-warn {{ background: rgba(250,178,25,0.08); border-color: rgba(250,178,25,0.30); }}
    .hero-card.hero-neutral {{ background: {SURFACE}; }}
    .hero-eyebrow {{
        color: {INK_MUTED};
        font-size: 0.78rem;
        font-weight: 600;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        margin-bottom: 10px;
    }}
    .hero-figure {{
        color: {INK_PRIMARY};
        font-size: 2.6rem;
        font-weight: 650;
        line-height: 1.05;
        margin-bottom: 6px;
    }}
    .hero-detail {{ color: {INK_SECONDARY}; font-size: 0.95rem; }}

    /* --- Info card (spike, context) --- */
    .info-card {{
        background: {SURFACE};
        border: 1px solid {BORDER};
        border-radius: 12px;
        padding: 14px 18px;
        color: {INK_SECONDARY};
        font-size: 0.9rem;
        margin-bottom: 8px;
    }}
    .info-card b {{ color: {INK_PRIMARY}; }}

    /* Badges */
    .badge {{
        display: inline-block;
        padding: 2px 10px;
        border-radius: 999px;
        font-size: 0.78rem;
        font-weight: 600;
    }}
    .badge-go {{ background: rgba(12,163,12,0.18); color: #4ade80; }}
    .badge-modify {{ background: rgba(250,178,25,0.18); color: #fbbf24; }}
    .badge-nogo {{ background: rgba(208,59,59,0.18); color: #f87171; }}

    section[data-testid="stSidebar"] {{ display: none; }}
    </style>
    """,
    unsafe_allow_html=True,
)


def render_stat_cards(cards: list[dict]) -> None:
    """cards: [{"label": str, "value": str, "sub": str | None}]"""
    parts = ['<div class="stat-grid">']
    for card in cards:
        parts.append('<div class="stat-card">')
        parts.append(f'<div class="stat-label">{card["label"]}</div>')
        parts.append(f'<div class="stat-value">{card["value"]}</div>')
        if card.get("sub"):
            parts.append(f'<div class="stat-sub">{card["sub"]}</div>')
        parts.append("</div>")
    parts.append("</div>")
    st.markdown("".join(parts), unsafe_allow_html=True)


def style_fig(fig: go.Figure, height: int = 380) -> go.Figure:
    """Apply the dashboard's dark chart theme — surface, gridlines, font, hover style."""
    fig.update_layout(
        paper_bgcolor=SURFACE,
        plot_bgcolor=SURFACE,
        font=dict(family=FONT_STACK, color=INK_SECONDARY, size=12),
        height=height,
        margin=dict(l=10, r=10, t=36, b=10),
        legend=dict(bgcolor="rgba(0,0,0,0)", orientation="h", yanchor="bottom", y=1.02, xanchor="left", x=0, font=dict(color=INK_SECONDARY)),
        hoverlabel=dict(bgcolor=SURFACE, font_color=INK_PRIMARY, bordercolor=BORDER),
        title=dict(font=dict(color=INK_PRIMARY, size=14)),
    )
    fig.update_xaxes(gridcolor=GRIDLINE, zerolinecolor=BASELINE, showline=False, linecolor=BASELINE, tickfont=dict(color=INK_MUTED))
    fig.update_yaxes(gridcolor=GRIDLINE, zerolinecolor=BASELINE, showline=False, linecolor=BASELINE, tickfont=dict(color=INK_MUTED))
    return fig


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
    result_df = pd.DataFrame(
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
    # Some columns (break-even, execution probability) are only computed for
    # a subset of strategies — if a display window happens to contain none
    # of those rows, pandas infers the column as `object` dtype from an
    # all-None list instead of float64, and Styler's numeric format string
    # then breaks on the raw `None` (unlike NaN, which it handles fine).
    # Forcing numeric dtype here converts None -> NaN regardless.
    if not result_df.empty:
        numeric_columns = ["Gain brut (%)", "Seuil de rentabilité (%)", "Gain net (%)", "Résultat sur 1000 $", "Proba. exécution"]
        result_df[numeric_columns] = result_df[numeric_columns].apply(pd.to_numeric, errors="coerce")
    return result_df


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


# price_snapshots grows fast (event-driven detection writes a tick almost
# every scan). Charting only needs enough points to fill 1-15min candles —
# capping the row count bounds worst-case query time, pandas memory, and
# resample cost regardless of how large the table gets or how long a
# lookback window is requested. Ordered newest-first to keep the *most
# recent* N points, then re-ascended for charting.
MAX_PRICE_HISTORY_ROWS = 20_000
MAX_BID_ASK_HISTORY_ROWS = 150_000


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
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(MAX_PRICE_HISTORY_ROWS)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    history_df = pd.DataFrame(
        [
            {
                "exchange": r.exchange,
                "recorded_at": r.recorded_at,
                "mid": (float(r.bid) + float(r.ask)) / 2,
            }
            for r in rows
        ]
    )
    return history_df.sort_values("recorded_at") if not history_df.empty else history_df


async def fetch_bid_ask_history(symbols: list[str], hours: float = 2.0) -> pd.DataFrame:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        cutoff = (datetime.now(UTC) - timedelta(hours=hours)).replace(tzinfo=None)
        async with session_factory() as session:
            result = await session.execute(
                select(PriceSnapshot)
                .where(PriceSnapshot.symbol.in_(symbols), PriceSnapshot.recorded_at >= cutoff)
                .order_by(PriceSnapshot.recorded_at.desc())
                .limit(MAX_BID_ASK_HISTORY_ROWS)
            )
            rows = result.scalars().all()
    finally:
        await engine.dispose()
    bid_ask_df = pd.DataFrame(
        [
            {"symbol": r.symbol, "exchange": r.exchange, "recorded_at": r.recorded_at, "bid": float(r.bid), "ask": float(r.ask)}
            for r in rows
        ]
    )
    return bid_ask_df.sort_values("recorded_at") if not bid_ask_df.empty else bid_ask_df


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


# Reference portfolio for the headline Capital Rotation KPI — matches the
# Fast-Rotation spec's own worked examples (section 6-9, 58), which all use $5,000.
ROTATION_REFERENCE_PORTFOLIO = "5K"


async def fetch_rotation_report(mode: str | None, hours: float = 24.0) -> RotationReport | None:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(VirtualPortfolioRecord).where(VirtualPortfolioRecord.name == ROTATION_REFERENCE_PORTFOLIO)
            )
            portfolio = result.scalar_one_or_none()
            if portfolio is None:
                return None
            return await build_rotation_report(
                session, portfolio.id, portfolio.name, float(portfolio.initial_capital_usd), mode=mode, hours=hours
            )
    finally:
        await engine.dispose()


@st.cache_data(ttl=60)
def get_rotation_report_cached(mode: str | None, hours: float = 24.0) -> RotationReport | None:
    return asyncio.run(fetch_rotation_report(mode, hours))


async def fetch_holding_time_performance(hours: float = 24.0) -> list[HoldingTimeBucketStats]:
    engine = create_async_engine(get_settings().database_url)
    try:
        session_factory = async_sessionmaker(engine, expire_on_commit=False)
        async with session_factory() as session:
            result = await session.execute(
                select(VirtualPortfolioRecord).where(VirtualPortfolioRecord.name == ROTATION_REFERENCE_PORTFOLIO)
            )
            portfolio = result.scalar_one_or_none()
            if portfolio is None:
                return []
            return await build_holding_time_performance(session, portfolio.id, hours=hours)
    finally:
        await engine.dispose()


@st.cache_data(ttl=60)
def get_holding_time_performance_cached(hours: float = 24.0) -> list[HoldingTimeBucketStats]:
    return asyncio.run(fetch_holding_time_performance(hours))


df = asyncio.run(fetch_opportunities())
profitable = df[df["Gain net (%)"] > 0] if not df.empty else df

st.markdown(
    '<div style="font-size:1.9rem;font-weight:650;">🤖 Robot d\'arbitrage crypto</div>'
    f'<div style="color:{INK_SECONDARY};margin-top:2px;margin-bottom:18px;">'
    "Le robot compare en continu les prix sur Binance, OKX et Bybit — aucun argent réel n'est engagé (simulation uniquement)."
    "</div>",
    unsafe_allow_html=True,
)

# --- Bandeau de statut (hero card) ---
if df.empty:
    st.markdown(
        '<div class="hero-card hero-neutral"><div class="hero-eyebrow">Statut</div>'
        '<div class="hero-figure">Démarrage…</div>'
        '<div class="hero-detail">Le robot vient de démarrer, pas encore de données à afficher. Reviens dans quelques instants.</div></div>',
        unsafe_allow_html=True,
    )
elif not profitable.empty:
    best = profitable.sort_values("Résultat sur 1000 $", ascending=False).iloc[0]
    st.markdown(
        '<div class="hero-card hero-good"><div class="hero-eyebrow">✅ Opportunité rentable maintenant</div>'
        f'<div class="hero-figure">{best["Résultat sur 1000 $"]:+.2f} $ <span style="font-size:1.1rem;color:{INK_SECONDARY};font-weight:500;">sur 1000 $</span></div>'
        f'<div class="hero-detail">{len(profitable)} opportunité(s) rentable(s) au total — la meilleure : '
        f'<b>{best["Paire"]}</b> ({best["Stratégie"]})</div></div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        '<div class="hero-card hero-warn"><div class="hero-eyebrow">❌ Rien de rentable pour l\'instant</div>'
        '<div class="hero-figure" style="font-size:1.5rem;">Les écarts sont trop petits pour couvrir les frais</div>'
        '<div class="hero-detail">C\'est normal et fréquent — le robot continue de chercher 24h/24, il suffit qu\'un écart plus grand apparaisse.</div></div>',
        unsafe_allow_html=True,
    )

# --- Dernier pic rentable (historique, pas l'instant présent) ---
last_spike = get_last_profitable_spike_cached()
if last_spike is None:
    st.markdown(
        '<div class="info-card">🎯 Aucun pic rentable détecté depuis le début de l\'observation — c\'est rare, pas un problème.</div>',
        unsafe_allow_html=True,
    )
else:
    st.markdown(
        f'<div class="info-card">🎯 Dernier pic rentable détecté : <b>{humanize_delta(last_spike["detected_at"])}</b> '
        f'({last_spike["detected_at"].strftime("%d/%m %H:%M:%S")} UTC) — '
        f'<b>{last_spike["symbol"]}</b> ({last_spike["strategy"]}), '
        f'<b>+{last_spike["net_spread_pct"]:.2f}% net</b>. '
        f'{last_spike["count_24h"]} pic(s) rentable(s) sur les dernières 24h.</div>',
        unsafe_allow_html=True,
    )

# --- Chiffres clés ---
render_stat_cards(
    [
        {"label": "Opportunités repérées", "value": f"{len(df):,}".replace(",", " "), "sub": "récentes"},
        {"label": "Dont rentables après frais", "value": f"{len(profitable) if not df.empty else 0:,}".replace(",", " ")},
        {
            "label": "Meilleur résultat sur 1000 $",
            "value": f"{df['Résultat sur 1000 $'].max():+.2f} $" if not df.empty else "—",
        },
    ]
)

st.divider()

# --- Fast Rotation Mode : le nouveau KPI principal ---
st.markdown(
    '<div style="font-size:1.3rem;font-weight:650;">⚡ Fast Rotation Mode</div>'
    f'<div style="color:{INK_SECONDARY};margin-top:2px;margin-bottom:14px;">'
    "Objectif : réutiliser le même capital plusieurs fois plutôt que l'immobiliser longtemps. "
    f"Référence : portefeuille {ROTATION_REFERENCE_PORTFOLIO} — Carry Mode (Basis/Financement) suivi séparément."
    "</div>",
    unsafe_allow_html=True,
)

fast_report = get_rotation_report_cached(mode="fast")
carry_report = get_rotation_report_cached(mode="carry")

if fast_report is None:
    st.info("Portefeuille de référence introuvable — le robot vient peut-être de démarrer.")
else:
    holding_display = f"{fast_report.avg_holding_time_seconds:.0f} sec" if fast_report.avg_holding_time_seconds else "—"
    render_stat_cards(
        [
            {"label": "Capital utilisé (24h)", "value": f"{fast_report.total_capital_traded_usd:,.0f} $".replace(",", " ")},
            {"label": "Rotation de capital", "value": f"{fast_report.capital_rotation_rate:.1f}x", "sub": f"sur {fast_report.base_capital_usd:,.0f} $".replace(",", " ")},
            {"label": "Trades Fast (24h)", "value": f"{fast_report.completed_trades:,}".replace(",", " "), "sub": f"{fast_report.trades_per_hour:.1f} / heure"},
            {"label": "Détention moyenne", "value": holding_display},
            {"label": "P&L net Fast (24h)", "value": f"{fast_report.net_pnl_usd:+.2f} $"},
        ]
    )
    if carry_report and carry_report.completed_trades > 0:
        st.caption(
            f"Carry Mode (Basis/Financement, à part) : {carry_report.completed_trades} trade(s), "
            f"P&L net {carry_report.net_pnl_usd:+.2f} $, détention moyenne "
            f"{carry_report.avg_holding_time_seconds / 86400:.1f} jour(s)"
            if carry_report.avg_holding_time_seconds
            else f"Carry Mode (Basis/Financement, à part) : {carry_report.completed_trades} trade(s), P&L net {carry_report.net_pnl_usd:+.2f} $"
        )

    # --- Performance par horizon de détention (section 45) ---
    buckets = get_holding_time_performance_cached()
    if buckets:
        st.markdown(
            f'<div style="font-size:1rem;font-weight:600;margin-top:16px;margin-bottom:8px;">Performance par horizon de détention</div>'
            f'<div style="color:{INK_SECONDARY};font-size:0.85rem;margin-bottom:10px;">'
            "Quel horizon (Ultra Fast / Fast / Medium / Carry) rapporte vraiment, plutôt qu'un seul total qui peut masquer un horizon perdant."
            "</div>",
            unsafe_allow_html=True,
        )
        bucket_rows_html = []
        for b in buckets:
            pnl_color = STATUS_GOOD if b.net_pnl_usd >= 0 else STATUS_CRITICAL
            holding_display = (
                f"{b.avg_holding_time_seconds / 86400:.1f} j" if b.avg_holding_time_seconds and b.avg_holding_time_seconds > 3600
                else f"{b.avg_holding_time_seconds:.0f} s" if b.avg_holding_time_seconds else "—"
            )
            trade_count_display = f"{b.trade_count:,}".replace(",", " ")
            bucket_rows_html.append(
                "<tr>"
                f'<td style="padding:10px 14px;color:{INK_PRIMARY};">{HOLDING_TIME_LABELS.get(b.holding_time_category, b.holding_time_category)}</td>'
                f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{trade_count_display}</td>'
                f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{b.win_rate_pct:.1f} %</td>'
                f'<td style="padding:10px 14px;color:{pnl_color};text-align:right;font-weight:600;">{b.net_pnl_usd:+.2f} $</td>'
                f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{b.avg_net_profit_per_trade_usd:+.3f} $</td>'
                f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{holding_display}</td>'
                "</tr>"
            )
        bucket_table_html = f"""
        <div style="background:{SURFACE};border:1px solid {BORDER};border-radius:14px;overflow:hidden;">
        <table style="width:100%;border-collapse:collapse;font-size:0.88rem;">
        <thead>
            <tr style="border-bottom:1px solid {GRIDLINE};">
                <th style="padding:10px 14px;text-align:left;color:{INK_MUTED};font-weight:600;">Horizon</th>
                <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Trades</th>
                <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Taux de réussite</th>
                <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">P&L net</th>
                <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Moyenne / trade</th>
                <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Détention moy.</th>
            </tr>
        </thead>
        <tbody>{"".join(bucket_rows_html)}</tbody>
        </table>
        </div>
        """
        st.markdown(bucket_table_html, unsafe_allow_html=True)

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
                    increasing_line_color=STATUS_GOOD,
                    increasing_fillcolor=STATUS_GOOD,
                    decreasing_line_color=STATUS_CRITICAL,
                    decreasing_fillcolor=STATUS_CRITICAL,
                )
            ]
        )
        candle_fig.update_layout(title=f"{chart_symbol} — {candle_exchange}", xaxis_rangeslider_visible=False, showlegend=False)
        st.plotly_chart(style_fig(candle_fig, height=420), use_container_width=True)

    st.caption("Comparaison du prix sur les 3 plateformes — un écart visible entre les lignes, c'est une opportunité d'arbitrage.")
    compare_fig = go.Figure()
    for exchange in exchanges_present:
        sub = price_df[price_df["exchange"] == exchange]
        compare_fig.add_trace(
            go.Scatter(
                x=sub["recorded_at"],
                y=sub["mid"],
                mode="lines",
                name=exchange.capitalize(),
                line=dict(color=EXCHANGE_COLORS.get(exchange, SEQUENTIAL_BLUE), width=2),
            )
        )
    st.plotly_chart(style_fig(compare_fig, height=320), use_container_width=True)

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

        render_stat_cards(
            [
                {"label": "Instants rentables (en espérance)", "value": f"{positive_share:.1f} %"},
                {"label": "Résultat moyen sur 1000 $ (en espérance)", "value": f"{avg_ev:+.2f} $"},
            ]
        )

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
            .sort_values(ascending=True)
            .tail(10)
        )
        pairs_fig = go.Figure(
            go.Bar(
                x=top_pairs.values,
                y=top_pairs.index,
                orientation="h",
                marker_color=[SEQUENTIAL_BLUE if v >= 0 else STATUS_CRITICAL for v in top_pairs.values],
                marker_line_width=0,
            )
        )
        pairs_fig.update_xaxes(title="Valeur espérée sur 1000 $")
        st.plotly_chart(style_fig(pairs_fig, height=max(220, 34 * len(top_pairs))), use_container_width=True)

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
            color = f"background-color: {SURFACE}"
        elif net > 0:
            color = "background-color: rgba(12,163,12,0.12)"
        else:
            color = "background-color: rgba(208,59,59,0.10)"
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
    counts = df["Stratégie"].value_counts().sort_values(ascending=True)
    strategy_fig = go.Figure(go.Bar(x=counts.values, y=counts.index, orientation="h", marker_color=SEQUENTIAL_BLUE, marker_line_width=0))
    st.plotly_chart(style_fig(strategy_fig, height=max(200, 46 * len(counts))), use_container_width=True)

st.divider()

# --- Rapport quotidien ---
st.subheader("📅 Résumé du jour (24 dernières heures)")
daily = get_daily_summary_cached()

render_stat_cards(
    [
        {"label": "Opportunités repérées", "value": f"{daily.detected:,}".replace(",", " ")},
        {"label": "Dont rentables (net > 0)", "value": f"{daily.net_positive:,}".replace(",", " ")},
        {"label": "Trades simulés", "value": f"{daily.paper_trades:,}".replace(",", " ")},
        {
            "label": "P&L simulé cumulé",
            "value": f"{daily.simulated_net_pnl_usd:+.2f} $",
        },
    ]
)

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

render_stat_cards(
    [
        {"label": "Opportunités (7j)", "value": f"{weekly.total_opportunities:,}".replace(",", " ")},
        {"label": "Trades exécutés (simulés)", "value": f"{weekly.executed_simulations:,}".replace(",", " ")},
        {"label": "Occasions manquées", "value": f"{weekly.missed_opportunities:,}".replace(",", " ")},
        {"label": "P&L simulé (7j)", "value": f"{weekly.net_simulated_pnl_usd:+.2f} $"},
    ]
)

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
    verdict_badge_html = {
        Verdict.GO: '<span class="badge badge-go">🟢 GO</span>',
        Verdict.MODIFY: '<span class="badge badge-modify">🟠 MODIFY</span>',
        Verdict.NO_GO: '<span class="badge badge-nogo">🔴 NO-GO</span>',
    }
    ranked = sorted(weekly.by_strategy, key=lambda s: s.avg_net_return_pct, reverse=True)

    rows_html = []
    for s in ranked:
        rows_html.append(
            "<tr>"
            f'<td style="padding:10px 14px;color:{INK_PRIMARY};">{STRATEGY_LABELS.get(s.strategy, s.strategy)}</td>'
            f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{s.total_opportunities:,}</td>'
            f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{s.net_positive_rate * 100:.2f} %</td>'
            f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{s.avg_net_return_pct:.4f} %</td>'
            f'<td style="padding:10px 14px;color:{INK_SECONDARY};text-align:right;">{s.median_net_return_pct:.4f} %</td>'
            f'<td style="padding:10px 14px;text-align:center;">{verdict_badge_html[s.verdict]}</td>'
            "</tr>"
        )

    table_html = f"""
    <div style="background:{SURFACE};border:1px solid {BORDER};border-radius:14px;overflow:hidden;">
    <table style="width:100%;border-collapse:collapse;font-size:0.88rem;">
    <thead>
        <tr style="border-bottom:1px solid {GRIDLINE};">
            <th style="padding:10px 14px;text-align:left;color:{INK_MUTED};font-weight:600;">Stratégie</th>
            <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Opportunités</th>
            <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Rentables</th>
            <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Rendement moyen</th>
            <th style="padding:10px 14px;text-align:right;color:{INK_MUTED};font-weight:600;">Rendement médian</th>
            <th style="padding:10px 14px;text-align:center;color:{INK_MUTED};font-weight:600;">Verdict</th>
        </tr>
    </thead>
    <tbody>{"".join(rows_html)}</tbody>
    </table>
    </div>
    """
    st.markdown(table_html, unsafe_allow_html=True)

st.divider()
st.caption(f"Plateformes surveillées : {', '.join(e.capitalize() for e in PRIORITY_EXCHANGES)}")
