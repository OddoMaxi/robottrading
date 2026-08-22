"""Expert Mode — the full technical dashboard (net edge, gross spread, maker/taker,
VWAP, orderbook, latency, slippage, funding, partial fills, opportunity score,
execution probability, strategy analytics). Nothing here is simplified; Simple
Mode is the new default, this is the "show me everything" view (spec section 28).
"""

import asyncio
from datetime import UTC, datetime

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from app.analytics.fees import FeeEngine
from app.analytics.maker_simulation import MakerAssumptions, best_maker_pair
from app.config.constants import CROSS_EXCHANGE_ASSETS, PRIORITY_EXCHANGES
from app.config.fees import DEFAULT_FEE_SCHEDULES, uniform_fee_schedules

import dashboard.data as data
from dashboard.theme import (
    ATTEMPT_OUTCOME_LABELS,
    BORDER,
    EXCHANGE_COLORS,
    GRIDLINE,
    HOLDING_TIME_LABELS,
    ILLUSTRATIVE_CAPITAL_USD,
    INK_MUTED,
    INK_PRIMARY,
    INK_SECONDARY,
    SEQUENTIAL_BLUE,
    STATUS_CRITICAL,
    STATUS_GOOD,
    REJECTION_REASON_LABELS,
    STRATEGY_LABELS,
    SURFACE,
    humanize_delta,
    render_stat_cards,
    style_fig,
)
from app.reporting.weekly import Verdict


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


def _format_seconds(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"~{int(seconds)} s"
    if seconds < 3600:
        return f"~{int(seconds / 60)} min"
    if seconds < 86400:
        return f"~{seconds / 3600:.1f} h"
    return f"~{seconds / 86400:.1f} j"


def render_expert_mode() -> None:
    df = data.get_opportunities_cached()
    profitable = df[df["Gain net (%)"] > 0] if not df.empty else df

    st.markdown(
        '<div style="font-size:1.9rem;font-weight:650;">🤖 Robot d\'arbitrage crypto — Mode Expert</div>'
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
    last_spike = data.get_last_profitable_spike_cached()
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
        f"Référence : portefeuille {data.ROTATION_REFERENCE_PORTFOLIO} — Carry Mode (Basis/Financement) suivi séparément."
        "</div>",
        unsafe_allow_html=True,
    )

    fast_report = data.get_rotation_report_cached(mode="fast")
    carry_report = data.get_rotation_report_cached(mode="carry")

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

        # --- Performance par horizon de détention ---
        buckets = data.get_holding_time_performance_cached()
        if buckets:
            st.markdown(
                '<div style="font-size:1rem;font-weight:600;margin-top:16px;margin-bottom:8px;">Performance par horizon de détention</div>'
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

    price_df = asyncio.run(data.fetch_price_history(chart_symbol))

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

    maker_raw_df = data.get_bid_ask_history_cached(tuple(chart_symbols), maker_hours)

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
        display_df = df.sort_values("_detected_at", ascending=False).drop(
            columns=[c for c in df.columns if c.startswith("_")]
        ).head(50)

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
    daily = data.get_daily_summary_cached()

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

    # --- Drawdown & Profit Factor (Reality Engine spec, sections 34-35) ---
    st.subheader("📉 Plus forte baisse & Profit Factor")
    st.caption("Un robot ne se juge pas uniquement au profit — ces deux chiffres montrent le risque traversé pour l'obtenir.")
    metrics = data.get_performance_metrics_cached(hours=24.0)
    if metrics is None:
        st.info("Portefeuille de référence introuvable — le robot vient peut-être de démarrer.")
    else:
        profit_factor_display = f"{metrics.profit_factor:.2f}" if metrics.profit_factor is not None else "—"
        render_stat_cards(
            [
                {"label": "Plus forte baisse ($)", "value": f"{metrics.max_drawdown_usd:+.2f} $"},
                {"label": "Plus forte baisse (%)", "value": f"{metrics.max_drawdown_pct:+.2f} %", "sub": f"pic à {metrics.peak_capital_usd:,.2f} $".replace(",", " ")},
                {"label": "Profit Factor", "value": profit_factor_display, "sub": "gains bruts / pertes brutes"},
                {"label": "Gains bruts / Pertes brutes", "value": f"{metrics.gross_winning_usd:+.2f} $ / {metrics.gross_losing_usd:+.2f} $"},
            ]
        )
        with st.expander("Comment lire ces deux chiffres ?"):
            st.markdown(
                """
                - **Plus forte baisse (Maximum Drawdown)** : la plus grosse chute entre un sommet et un creux du capital sur la période — même un robot rentable peut traverser une chute intermédiaire importante avant de se redresser.
                - **Profit Factor** : gains bruts cumulés des trades gagnants, divisés par la valeur absolue des pertes brutes cumulées des trades perdants. Au-dessus de 1 = plus gagné que perdu ; en dessous de 1 = l'inverse. « — » s'affiche s'il n'y a pas encore eu de trade perdant sur la période (division par zéro évitée, pas un chiffre inventé).
                """
            )

    st.divider()

    # --- "Pourquoi le robot ne trade pas ?" (user request — live diagnostic,
    # expanded to the EXECUTION INACTIVITY DIAGNOSTIC spec, 2026-08-21) ---
    st.subheader("🩺 Pourquoi le robot ne trade pas ?")
    st.caption(
        "Diagnostic en direct depuis le dernier trade réellement exécuté sur le portefeuille de référence (5K) — "
        "distingue « le marché ne donne rien » de « le robot trouve des opportunités mais ne peut pas les prendre »."
    )
    why_report = data.get_why_no_trade_cached()
    if why_report is None:
        st.info("Portefeuille de référence introuvable.")
    elif why_report.last_trade_at is None or why_report.funnel is None:
        st.info("Aucun trade exécuté pour l'instant sur ce portefeuille.")
    else:
        funnel = why_report.funnel
        market_events_display = (
            f"{why_report.market_events_since_last_trade:,}".replace(",", " ")
            if why_report.market_events_since_last_trade is not None
            else "—"
        )
        render_stat_cards(
            [
                {"label": "Dernier trade", "value": humanize_delta(why_report.last_trade_at)},
                {"label": "Ticks de marché depuis", "value": market_events_display, "sub": "tous exchanges confondus"},
                {"label": "Opportunités uniques détectées", "value": f"{funnel.stage('detected').count:,}".replace(",", " ")},
                {"label": "Positives après coûts", "value": f"{funnel.stage('profitable_after_fees').count:,}".replace(",", " "), "sub": "écart net > 0 (frais + coût VWAP)"},
                {"label": "Rentables", "value": f"{funnel.stage('profitable').count:,}".replace(",", " "), "sub": "au-dessus du seuil minimal jugé valable"},
                {"label": "Exécutables", "value": f"{funnel.stage('executable').count:,}".replace(",", " "), "sub": "a passé toute la validation"},
                {"label": "Tentatives d'exécution", "value": f"{funnel.execution_attempts:,}".replace(",", " "), "sub": "sur les 5 portefeuilles, tous statuts"},
                {"label": "Rejetées", "value": f"{funnel.stage('rejected').count:,}".replace(",", " ")},
            ]
        )

        if funnel.rejection_reasons:
            st.markdown('<div class="simple-card-label" style="margin-top:14px;">Répartition des rejets (avant tentative — niveau opportunité)</div>', unsafe_allow_html=True)
            reason_rows = "".join(
                f'<div class="simple-perf-row"><span class="k">{REJECTION_REASON_LABELS.get(reason, reason)}</span>'
                f'<span class="v">{count:,} <span style="color:{INK_MUTED};font-weight:500;">({pct:.1f} %)</span></span></div>'.replace(",", " ")
                for reason, count, pct in funnel.rejection_reasons
            )
            st.markdown(f'<div class="simple-card">{reason_rows}</div>', unsafe_allow_html=True)
            st.caption(
                "5 raisons réellement suivies aujourd'hui par le moteur (app.execution.validator) — "
                "aucune catégorie inventée. « Écart trop faible » couvre à la fois net ≤ 0 et net positif mais "
                "sous le seuil minimal ; slippage, liquidité et notionnel minimum ne sont pas des filtres de rejet "
                "distincts dans le code actuel (le slippage s'applique après exécution, sur le P&L réalisé)."
            )

        if funnel.attempt_outcomes:
            st.markdown(
                '<div class="simple-card-label" style="margin-top:14px;">Résultat des tentatives (après validation — niveau exécution)</div>',
                unsafe_allow_html=True,
            )
            outcome_rows = "".join(
                f'<div class="simple-perf-row"><span class="k">{ATTEMPT_OUTCOME_LABELS.get(status, status)}</span>'
                f'<span class="v">{count:,} <span style="color:{INK_MUTED};font-weight:500;">({pct:.1f} %)</span></span></div>'.replace(",", " ")
                for status, count, pct in funnel.attempt_outcomes
            )
            st.markdown(f'<div class="simple-card">{outcome_rows}</div>', unsafe_allow_html=True)
            st.caption(
                "« Capital indisponible » et « limite de positions atteinte » sont la vraie réponse à "
                "« le robot trouve des opportunités mais ne peut pas les prendre » : une opportunité approuvée est "
                "toujours tentée sur les 5 portefeuilles, indépendamment du capital disponible sur chacun."
            )

        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Capital</div>', unsafe_allow_html=True)
        available = why_report.capital.total_capital_usd - why_report.capital.engaged_usd
        render_stat_cards(
            [
                {"label": "Total", "value": f"{why_report.capital.total_capital_usd:,.2f} $".replace(",", " ")},
                {"label": "Disponible", "value": f"{available:,.2f} $".replace(",", " ")},
                {
                    "label": "Engagé / Réservé",
                    "value": f"{why_report.capital.engaged_usd:,.2f} $".replace(",", " "),
                    "sub": f"{why_report.capital.utilization_pct:.0f} % du capital",
                },
                {"label": "Positions ouvertes", "value": f"{why_report.capital.open_position_count}"},
            ]
        )
        st.caption(
            "Le modèle actuel n'a pas d'état « réservé » distinct de « engagé » : le capital d'une position "
            "ouverte est verrouillé (= engagé = réservé, un seul et même état), sinon disponible."
        )

        if why_report.open_positions:
            st.markdown('<div class="simple-card-label" style="margin-top:14px;">Positions ouvertes</div>', unsafe_allow_html=True)
            position_rows = []
            for p in why_report.open_positions:
                remaining_seconds = max(0.0, (p.closes_at - datetime.now(UTC).replace(tzinfo=None)).total_seconds())
                remaining_days = remaining_seconds / 86400.0
                remaining_display = f"~{remaining_days:.1f} j" if remaining_days >= 1 else f"~{int(remaining_seconds / 60)} min"
                position_rows.append(
                    f'<div class="simple-perf-row"><span class="k">{p.strategy} · {p.symbol}</span>'
                    f'<span class="v">{p.capital_usd:,.2f} $ — ferme dans {remaining_display}</span></div>'.replace(",", " ")
                )
            st.markdown(f'<div class="simple-card">{"".join(position_rows)}</div>', unsafe_allow_html=True)

        st.markdown('<div class="simple-card-label" style="margin-top:14px;">État du moteur</div>', unsafe_allow_html=True)
        health_icon = {"running": "🟢", "degraded": "🟡", "down": "🔴"}[why_report.robot_status.health.value]
        exchanges_text = " · ".join(
            f"{name.capitalize()} {'✓' if ok else '✕'}" for name, ok in why_report.robot_status.exchanges_connected.items()
        )
        st.markdown(f"{health_icon} **{why_report.robot_status.health.value}** — {exchanges_text}")

    # --- FAST TRADING ONLY — holding-time compliance (user directive, 2026-08-21) ---
    st.markdown('<div class="simple-card-label" style="margin-top:14px;">Profil de durée de détention (24h) — FAST TRADING ONLY</div>', unsafe_allow_html=True)
    dist = data.get_holding_time_distribution_cached(hours=24.0)
    if dist is None or dist.trade_count == 0:
        st.info("Pas encore de trade sur la période pour mesurer le profil de durée.")
    else:
        render_stat_cards(
            [
                {"label": "Durée moyenne", "value": _format_seconds(dist.avg_holding_seconds)},
                {"label": "Durée médiane", "value": _format_seconds(dist.median_holding_seconds)},
                {"label": "% trades < 5 min", "value": f"{dist.pct_under_5min:.0f} %"},
                {"label": "% trades < 10 min", "value": f"{dist.pct_under_10min:.0f} %"},
                {"label": "% trades < 20 min", "value": f"{dist.pct_under_20min:.0f} %"},
                {
                    "label": "Trade le plus long",
                    "value": _format_seconds(dist.longest_holding_seconds),
                    "sub": f"{dist.longest_trade_symbol}" if dist.longest_trade_symbol else None,
                },
            ]
        )

    st.divider()

    # --- Execution Engine Audit — Funnel (pre-live-trading audit, user request) ---
    st.subheader("🔎 Audit du moteur d'exécution — entonnoir complet")
    st.caption(
        "Détectées → rentables après frais → rejetées / exécutables → exécutées → clôturées. "
        "Chaque pourcentage est relatif au nombre d'opportunités détectées (24h)."
    )
    funnel = data.get_execution_funnel_cached(hours=24.0)
    stage_labels = {
        "detected": "Détectées",
        "profitable_after_fees": "Rentables après frais",
        "profitable": "Rentables (au-dessus du seuil)",
        "rejected": "Rejetées",
        "executable": "Exécutables",
        "executed": "Exécutées",
        "closed": "Clôturées",
    }
    if funnel.stage("detected").count == 0:
        st.info("Aucune opportunité détectée sur la période — le robot vient peut-être de démarrer.")
    else:
        rows_html = "".join(
            f'<div class="simple-perf-row"><span class="k">{stage_labels[s.name]}</span>'
            f'<span class="v">{s.count:,} <span style="color:{INK_MUTED};font-weight:500;">({s.pct_of_detected:.1f} %)</span></span></div>'.replace(",", " ")
            for s in funnel.stages
        )
        st.markdown(f'<div class="simple-card">{rows_html}</div>', unsafe_allow_html=True)

        if funnel.rejection_reasons:
            st.markdown('<div class="simple-card-label" style="margin-top:14px;">Principales raisons de rejet</div>', unsafe_allow_html=True)
            reason_rows = "".join(
                f'<div class="simple-perf-row"><span class="k">{REJECTION_REASON_LABELS.get(reason, reason)}</span>'
                f'<span class="v">{count:,} <span style="color:{INK_MUTED};font-weight:500;">({pct:.1f} %)</span></span></div>'.replace(",", " ")
                for reason, count, pct in funnel.rejection_reasons
            )
            st.markdown(f'<div class="simple-card">{reason_rows}</div>', unsafe_allow_html=True)

        with st.expander("Comment lire cet entonnoir ?"):
            st.markdown(
                """
                - **Détectées** : événements économiques distincts (une même opportunité observée plusieurs fois d'affilée compte une seule fois).
                - **Rentables après frais** : l'écart net (après frais + coût VWAP) est strictement positif.
                - **Rentables (au-dessus du seuil)** : sous-ensemble du précédent — la classification dépasse le seuil minimal jugé valable (le même seuil qui déclenche le rejet « écart trop faible » en dessous).
                - **Rejetées** : n'a pas passé la validation (données trop anciennes, frais trop élevés, écart trop faible, position déjà ouverte, durée de détention trop longue).
                - **Exécutables** : a passé la validation — une allocation de capital aurait été tentée.
                - **Exécutées** : au moins un portefeuille a réellement simulé un trade dessus.
                - **Clôturées** : le trade exécuté a atteint la fin de sa durée de détention — résultat définitif.
                """
            )

    # --- FAST ROTATION & CAPITAL VELOCITY OPTIMIZER (user directive, 2026-08-21) ---
    st.markdown('<div class="simple-card-label" style="margin-top:14px;">Optimiseur de rotation du capital (portefeuille 5K, 24h)</div>', unsafe_allow_html=True)
    st.caption(
        "L'objectif n'est pas de maximiser le nombre de trades, mais le gain net réaliste par minute de capital "
        "immobilisé, sous risque contrôlé. Ces chiffres mesurent ce qui s'est réellement passé — jamais utilisés "
        "pour extrapoler qu'une opportunité va se répéter."
    )
    efficiency_report = data.get_rotation_report_cached(mode=None, hours=24.0)
    if efficiency_report is None or efficiency_report.completed_trades == 0:
        st.info("Pas encore assez de trades pour mesurer l'efficacité d'exécution.")
    else:
        trade_breakdown = data.get_trade_status_breakdown_cached(hours=24.0)
        attempts = (trade_breakdown.failed + trade_breakdown.closed + trade_breakdown.open) if trade_breakdown else 0
        failure_rate_display = f"{trade_breakdown.failed / attempts * 100:.1f} %" if attempts else "—"

        capital_util = data.get_capital_utilization_cached()
        capital_idle_display = f"{100 - capital_util.utilization_pct:.0f} %" if capital_util else "—"

        exec_funnel = data.get_execution_funnel_cached(hours=24.0)
        executable_count = exec_funnel.stage("executable").count
        executed_count = exec_funnel.stage("executed").count
        capture_rate_display = f"{executed_count / executable_count * 100:.1f} %" if executable_count else "—"

        capital_minute_display = (
            f"{efficiency_report.net_profit_per_capital_minute_usd:+.5f} $"
            if efficiency_report.net_profit_per_capital_minute_usd is not None
            else "—"
        )

        render_stat_cards(
            [
                {"label": "Trades / heure", "value": f"{efficiency_report.trades_per_hour:.1f} /h"},
                {"label": "Gain net / heure", "value": f"{efficiency_report.net_profit_per_hour_usd:+.2f} $"},
                {"label": "Rendement net / heure", "value": f"{efficiency_report.net_return_per_hour_pct:+.4f} %"},
                {"label": "Gain net / capital-minute", "value": capital_minute_display, "sub": "la métrique centrale de l'optimiseur"},
                {"label": "Rotation du capital", "value": f"{efficiency_report.capital_rotation_rate:.1f}×"},
                {"label": "Capital inactif", "value": capital_idle_display, "sub": "% du capital non engagé en ce moment"},
                {
                    "label": "Taux de capture",
                    "value": capture_rate_display,
                    "sub": f"{executed_count}/{executable_count} opportunités exécutables réellement exécutées",
                },
                {"label": "Taux d'échec", "value": failure_rate_display, "sub": "opportunités validées jamais exécutées"},
            ]
        )

    st.divider()

    # --- Micro Live Readiness (Reality Engine spec, sections 59-60) ---
    st.subheader("🚦 Micro Live Readiness — prêt pour un test contrôlé ?")
    st.caption(
        "Compose tous les indicateurs de santé déjà calculés (registre comptable, capital, résilience au stress, "
        "connectivité testnet) en un seul verdict. Une donnée manquante compte comme un échec, jamais comme un succès par défaut."
    )
    readiness = data.get_micro_live_readiness_cached()
    _READINESS_CHECK_LABELS = {
        "ledger_healthy": "Registre comptable cohérent",
        "no_negative_capital": "Capital jamais négatif",
        "no_over_allocation": "Jamais plus de 100% engagé",
        "kill_switch_disengaged": "Coupe-circuit désengagé",
        "reality_capture_stable": "Fiabilité de la simulation stable",
        "positive_net_pnl": "P&L net positif",
        "acceptable_drawdown": "Baisse maximale acceptable",
        "stress_test_positive": "Résiste au test de stress",
        "testnet_reachable": "Testnet Binance joignable",
    }
    if readiness.verdict is None:
        st.info("API du moteur injoignable — impossible de calculer le verdict pour le moment.")
    else:
        if readiness.verdict == "ready_for_controlled_test":
            st.success("✅ PRÊT POUR UN TEST CONTRÔLÉ")
        else:
            st.warning("⛔ PAS ENCORE PRÊT")
        for check in readiness.checks:
            icon = "✅" if check["passed"] else "❌"
            label = _READINESS_CHECK_LABELS.get(check["name"], check["name"])
            st.markdown(f"{icon} **{label}** — {check['detail']}")

    st.divider()

    # --- Rapport hebdomadaire + verdict GO/MODIFY/NO-GO ---
    st.subheader("📊 Bilan sur 7 jours — quelle stratégie garder ?")
    weekly = data.get_weekly_analytics_cached()

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

    # --- Multi-Market Opportunity Engine, V5.5 (user directive, 2026-08-21) ---
    st.subheader("🔗 On-Chain / DEX — Mode Simulation")
    st.caption(
        "Uniswap V3 (Ethereum), PancakeSwap V3 (BSC), Raydium et Orca (Solana) — données live, aucune transaction "
        "réelle. Moteur totalement isolé du CEX : un bug ici ne peut jamais arrêter ni corrompre le trading CEX."
    )
    freq = data.get_master_frequency_report_cached(hours=24.0)
    dex_freq = [s for s in freq.by_strategy if s.strategy in ("dex_cross", "dex_triangular", "dex_multihop", "atomic", "flash_loan_research")]
    if not dex_freq:
        st.info("Aucune opportunité on-chain détectée sur la période — le marché est peut-être simplement efficient (pas un bug).")
    else:
        total_detected = sum(s.detected_count for s in dex_freq)
        total_executable = sum(s.executable_count for s in dex_freq)
        render_stat_cards(
            [
                {"label": "Opportunités détectées (24h)", "value": f"{total_detected:,}".replace(",", " ")},
                {"label": "Réellement exécutables", "value": f"{total_executable:,}".replace(",", " "), "sub": "positives après TOUS les coûts"},
                {"label": "Détectées / heure", "value": f"{sum(s.detected_per_hour for s in dex_freq):.2f} /h"},
                {"label": "Exécutables / heure", "value": f"{sum(s.executable_per_hour for s in dex_freq):.2f} /h"},
            ]
        )

        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Répartition par stratégie on-chain</div>', unsafe_allow_html=True)
        dex_rows_html = "".join(
            f'<div class="simple-perf-row"><span class="k">{STRATEGY_LABELS.get(s.strategy, s.strategy)}</span>'
            f'<span class="v">{s.detected_count:,} détectées · {s.executable_count:,} exécutables · {s.executed_count:,} exécutées'
            f'</span></div>'.replace(",", " ")
            for s in sorted(dex_freq, key=lambda s: s.detected_count, reverse=True)
        )
        st.markdown(f'<div class="simple-card">{dex_rows_html}</div>', unsafe_allow_html=True)

        # DEX Execution Funnel — Detected -> ... -> Profitable/Losing (user
        # directive, 2026-08-22) — every DEX opportunity classed
        # "executable" now actually gets attempted against a real,
        # isolated shadow capital pool (app.onchain.dex_paper_trader),
        # never simulated_trades/VirtualPortfolio (spec section 39).
        exec_funnels = data.get_dex_execution_funnel_cached(hours=24.0)
        attemptable_funnels = [f for f in exec_funnels if f.strategy != "flash_loan_research"]
        if attemptable_funnels:
            st.markdown('<div class="simple-card-label" style="margin-top:14px;">Entonnoir d\'exécution DEX — du signal au profit réalisé (24h)</div>', unsafe_allow_html=True)
            headers = ["Stratégie", "Détectées", "Net+", "Exéc.", "Tentées", "Filled", "Edge disparu", "Échoués", "Rentables", "Perdants", "Capital $", "Profit net $", "$/cap-min"]
            rows_html = []
            for f in sorted(attemptable_funnels, key=lambda f: f.attempts, reverse=True):
                capital_minute_cell = (
                    f'{f.net_profit_per_capital_minute_usd:+.6f}'
                    if f.net_profit_per_capital_minute_usd is not None
                    else "—"
                )
                total_profit_color = STATUS_GOOD if f.total_net_profit_usd >= 0 else STATUS_CRITICAL
                row_html = (
                    "<tr>"
                    f'<td style="padding:8px 10px;color:{INK_PRIMARY};">{STRATEGY_LABELS.get(f.strategy, f.strategy)}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_SECONDARY};">{f.detected:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_SECONDARY};">{f.net_positive:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_SECONDARY};">{f.executable:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_SECONDARY};">{f.attempts:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{STATUS_GOOD};">{f.filled:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_MUTED};">{f.edge_disappeared:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{STATUS_CRITICAL};">{f.failed:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{STATUS_GOOD};">{f.profitable:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{STATUS_CRITICAL};">{f.losing:,}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_SECONDARY};">{f.capital_used_usd:,.0f}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{total_profit_color};">{f.total_net_profit_usd:+.4f}</td>'
                    f'<td style="padding:8px 10px;text-align:right;color:{INK_SECONDARY};">{capital_minute_cell}</td>'
                    "</tr>"
                ).replace(",", " ")
                rows_html.append(row_html)
            table_html = f"""
            <div style="background:{SURFACE};border:1px solid {BORDER};border-radius:14px;overflow-x:auto;">
            <table style="width:100%;border-collapse:collapse;font-size:0.82rem;white-space:nowrap;">
            <thead><tr style="border-bottom:1px solid {GRIDLINE};">
                {"".join(f'<th style="padding:8px 10px;text-align:right;color:{INK_MUTED};font-weight:600;">{h}</th>' if i > 0 else f'<th style="padding:8px 10px;text-align:left;color:{INK_MUTED};font-weight:600;">{h}</th>' for i, h in enumerate(headers))}
            </tr></thead>
            <tbody>{"".join(rows_html)}</tbody>
            </table>
            </div>
            """
            st.markdown(table_html, unsafe_allow_html=True)
            st.caption(
                "Filled = exécution simulée réussie · Edge disparu = revalidation juste avant exécution a détecté un edge devenu ≤ 0 "
                "(jamais compté comme un échec) · Échoués = tentative avec un vrai coût de gas, sans profit. "
                "flash_loan_research n'apparaît jamais ici : capital emprunté simulé, jamais de capital propre engagé (spec section 35)."
            )

        # DEX Reality Capture (spec section 22) — same philosophy as the
        # CEX Reality Capture Ratio above: how much of the raw, size-blind
        # edge actually survives real swap fees, gas, AMM price impact,
        # slippage, and MEV buffers.
        reality = data.get_dex_reality_capture_cached(hours=24.0)
        combined_reality = next((r for r in reality if r.strategy is None), None)
        if combined_reality is not None and combined_reality.capture_ratio_pct is not None:
            st.markdown('<div class="simple-card-label" style="margin-top:14px;">Fiabilité de la simulation on-chain</div>', unsafe_allow_html=True)
            render_stat_cards(
                [
                    {
                        "label": "Capture réelle",
                        "value": f"{combined_reality.capture_ratio_pct:.0f} %",
                        "sub": f"écart théorique moyen {combined_reality.avg_theoretical_edge_pct:.3f} % → réellement exécutable {combined_reality.avg_realistic_executable_edge_pct:.3f} %",
                    },
                ]
            )

        # Benchmark: CEX-only vs Multi-Market (spec section 38) — no
        # separate "before" run exists to replay (V5.5 was built and
        # deployed progressively, not toggled on wholesale at one instant)
        # — this compares CEX-only activity against combined CEX+DEX
        # activity within the SAME live window instead.
        benchmark = data.get_benchmark_report_cached(hours=24.0)
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Benchmark — CEX seul vs Multi-Market (24h)</div>', unsafe_allow_html=True)
        uplift_display = f"{benchmark.executable_per_hour_uplift_pct:+.0f} %" if benchmark.executable_per_hour_uplift_pct is not None else "—"
        render_stat_cards(
            [
                {"label": "CEX seul — exécutables/h", "value": f"{benchmark.cex_only.executable_per_hour:.2f} /h"},
                {"label": "DEX seul — exécutables/h", "value": f"{benchmark.dex_only.executable_per_hour:.2f} /h"},
                {"label": "Multi-Market combiné — exécutables/h", "value": f"{benchmark.combined.executable_per_hour:.2f} /h"},
                {"label": "Gain apporté par le DEX", "value": uplift_display, "sub": "opportunités exécutables/heure, CEX seul vs combiné"},
            ]
        )

    st.divider()
    st.caption(f"Plateformes surveillées : {', '.join(e.capitalize() for e in PRIORITY_EXCHANGES)}")
