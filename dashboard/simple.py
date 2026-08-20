"""Simple Mode — the default dashboard (Dashboard Simple V4 spec).

Answers, in under 5 seconds: is the robot running, how much capital is
there, what did it gain or lose today, how many trades happened, what is
it doing right now, is there a problem. Every number comes from the
existing engine's persisted data (spec section 22) — this module only
formats and narrates read-only aggregations already built in
dashboard/data.py and app/reporting/simple_summary.py. No trading,
strategy, or risk logic lives here (spec section 31).
"""

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dashboard.data as data
from app.config.constants import PRIORITY_EXCHANGES
from app.reporting.rotation import RotationReport
from app.reporting.simple_summary import CapitalUtilization, OpenPosition, build_explainer_narrative, pick_robot_state_message
from dashboard.theme import (
    INK_MUTED,
    INK_PRIMARY,
    INK_SECONDARY,
    REJECTION_REASON_LABELS,
    SEQUENTIAL_BLUE,
    STATUS_CRITICAL,
    STATUS_GOOD,
    STRATEGY_LABELS_SIMPLE,
    style_fig,
)

# Same set app.execution.validator.validate() uses to approve an
# opportunity for paper-trading — Simple Mode's "current opportunity" card
# only ever shows one that clears this bar.
GOOD_CLASSIFICATIONS = {"interesting", "good", "strong", "exceptional"}

CLASSIFICATION_BADGES = {
    "exceptional": ("🟢", "Excellente opportunité"),
    "strong": ("🟢", "Très bonne opportunité"),
    "good": ("🟢", "Bonne opportunité"),
    "interesting": ("🟡", "Opportunité correcte"),
}

NAV_PAGES = [("accueil", "Accueil"), ("trades", "Trades"), ("performance", "Performance"), ("parametres", "Paramètres")]


def describe_route(strategy: str, legs: list[dict]) -> str:
    """Plain "where does this trade happen" description — never mentions
    maker/taker, VWAP, or orderbook depth (spec section 9)."""
    if not legs:
        return STRATEGY_LABELS_SIMPLE.get(strategy, strategy)
    exchanges = sorted({leg.get("exchange") for leg in legs if leg.get("exchange")})
    markets = {leg.get("market") for leg in legs if leg.get("market")}
    if strategy == "triangular":
        exchange = exchanges[0].capitalize() if exchanges else "?"
        return f"Boucle triangulaire sur {exchange}"
    if len(exchanges) >= 2:
        return " ↔ ".join(e.capitalize() for e in exchanges)
    exchange = exchanges[0].capitalize() if exchanges else "?"
    if "perpetual" in markets:
        return f"{exchange} Spot ↔ Perpetual"
    if "futures" in markets:
        return f"{exchange} Spot ↔ Future"
    return exchange


def format_duration(seconds: float | None) -> str:
    if seconds is None:
        return "—"
    if seconds < 90:
        return f"~{int(seconds)} sec"
    if seconds < 3600:
        return f"~{int(seconds / 60)} min"
    if seconds < 86400:
        return f"~{seconds / 3600:.1f} h"
    return f"~{seconds / 86400:.1f} j"


def _money(value: float) -> str:
    return f"{value:,.2f} $".replace(",", " ")


# --- Header (spec sections 3, 15, 27) ---


def render_header(active_page: str) -> None:
    robot = data.get_robot_status_cached()
    status_class = {"running": "simple-status-running", "degraded": "simple-status-degraded", "down": "simple-status-down"}[robot.health.value]
    status_label = {"running": "🟢 EN MARCHE", "degraded": "🟡 SURVEILLANCE", "down": "🔴 PROBLÈME"}[robot.health.value]
    exchange_bits = " &nbsp;·&nbsp; ".join(
        f'<b>{name.capitalize()}</b> {"✓" if ok else "✕"}' for name, ok in robot.exchanges_connected.items()
    )

    st.markdown(
        f'<div class="simple-topbar"><div class="simple-brand">🤖 ROBOT</div>'
        f'<div class="simple-status-pill {status_class}">{status_label}</div></div>'
        f'<div class="simple-exchanges">{exchange_bits}</div>'
        '<div><span class="simple-sim-badge">MODE SIMULATION</span></div>',
        unsafe_allow_html=True,
    )

    nav_cols = st.columns(len(NAV_PAGES) + 1)
    for col, (key, label) in zip(nav_cols, NAV_PAGES):
        if col.button(label, key=f"nav_{key}", type="primary" if active_page == key else "secondary", use_container_width=True):
            st.session_state.simple_page = key
            st.rerun()
    if nav_cols[-1].button("Mode Expert →", key="nav_expert", use_container_width=True):
        st.session_state.mode = "expert"
        st.rerun()
    st.markdown("<div style='margin-bottom:6px;'></div>", unsafe_allow_html=True)


# --- Home cards (spec sections 4-14) ---


def render_capital_card(capital: float | None, utilization: CapitalUtilization | None = None) -> None:
    if capital is None:
        body = '<div class="simple-card-figure">—</div><div class="simple-card-sub">Pas encore disponible</div>'
    else:
        body = f'<div class="simple-card-figure">{_money(capital)}</div>'
        if utilization is not None:
            available = utilization.total_capital_usd - utilization.engaged_usd
            body += f'<div class="simple-card-sub">Disponible maintenant : <b>{_money(available)}</b></div>'
    st.markdown(f'<div class="simple-card"><div class="simple-card-label">Capital virtuel</div>{body}</div>', unsafe_allow_html=True)


def render_gain_card(capital: float | None, today: RotationReport | None) -> None:
    if capital is None or today is None or today.completed_trades == 0:
        body = '<div class="simple-card-figure">—</div><div class="simple-card-sub">Pas encore disponible</div>'
    else:
        pnl = today.net_pnl_usd
        base = capital - pnl
        pct = (pnl / base * 100) if base else 0.0
        tone = "good" if pnl >= 0 else "bad"
        body = f'<div class="simple-card-figure {tone}">{pnl:+,.2f} $</div>'.replace(",", " ") + f'<div class="simple-card-sub {tone}">{pct:+.2f} %</div>'
    st.markdown(f'<div class="simple-card"><div class="simple-card-label">Gain aujourd\'hui</div>{body}</div>', unsafe_allow_html=True)


def render_trades_rotation_grid(today: RotationReport | None, utilization: CapitalUtilization | None) -> None:
    if today is None or today.completed_trades == 0:
        trades_body = '<div class="simple-card-figure">0</div><div class="simple-card-sub">Aucun trade pour l\'instant</div>'
        rotation_body = '<div class="simple-card-figure">—</div>'
    else:
        trades_body = f'<div class="simple-card-figure">{today.completed_trades}</div><div class="simple-card-sub">{today.win_count} gagnants · {today.loss_count} perdants</div>'
        rotation_body = (
            f'<div class="simple-card-figure">{today.capital_rotation_rate:.1f}×</div>'
            f'<div class="simple-card-sub">Capital traité aujourd\'hui : {_money(today.total_capital_traded_usd)}</div>'
        )
    if utilization is None:
        utilization_body = '<div class="simple-card-figure">—</div>'
    else:
        utilization_body = (
            f'<div class="simple-card-figure">{utilization.utilization_pct:.0f} %</div>'
            f'<div class="simple-card-sub">{utilization.open_position_count} position(s) en cours</div>'
        )
    st.markdown(
        '<div class="simple-grid-2">'
        f'<div class="simple-card"><div class="simple-card-label">Trades</div>{trades_body}</div>'
        f'<div class="simple-card"><div class="simple-card-label">Rotation</div>{rotation_body}</div>'
        f'<div class="simple-card"><div class="simple-card-label">Utilisation actuelle</div>{utilization_body}</div>'
        "</div>",
        unsafe_allow_html=True,
    )


def render_positions_card(positions: list[OpenPosition]) -> None:
    """Continuous Execution spec, section 47 — no more than symbol / capital
    / booked P&L per position; full detail stays in Mode Expert."""
    if not positions:
        return
    rows = []
    for p in positions:
        asset = p.symbol.split("->")[0].split("/")[0]
        tone_color = STATUS_GOOD if p.net_profit_usd >= 0 else STATUS_CRITICAL
        rows.append(
            f'<div class="simple-opp-row"><span class="k">{asset}</span>'
            f'<span class="v">{_money(p.capital_usd)}</span>'
            f'<span class="v" style="color:{tone_color};margin-left:10px;">{p.net_profit_usd:+.2f} $</span></div>'
        )
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Positions en cours</div>' + "".join(rows) + "</div>",
        unsafe_allow_html=True,
    )


def render_state_card(daily) -> None:
    robot = data.get_robot_status_cached()
    msg = pick_robot_state_message(robot, daily.detected, daily.net_positive)
    st.markdown(
        f'<div class="simple-state-card {msg.tone}"><div class="simple-state-title">{msg.title}</div>'
        f'<div class="simple-state-body">{msg.body}</div></div>',
        unsafe_allow_html=True,
    )


def render_opportunity_card(df: pd.DataFrame) -> None:
    candidates = df[df["_classification"].isin(GOOD_CLASSIFICATIONS)] if not df.empty else df
    if df.empty or candidates.empty:
        st.markdown(
            '<div class="simple-card"><div class="simple-card-label">Opportunité actuelle</div>'
            '<div class="simple-card-sub">Aucune opportunité suffisamment rentable actuellement. '
            "Le robot continue de surveiller.</div></div>",
            unsafe_allow_html=True,
        )
        return

    row = candidates.iloc[0]
    route = describe_route(row["_strategy"], row["_legs"])
    emoji, label = CLASSIFICATION_BADGES.get(row["_classification"], ("🟡", "Opportunité correcte"))
    duration = format_duration(row["_holding_period_seconds"])
    capital_usd = row["_capital_usd"] or 0.0
    profit_usd = row["_expected_profit_usd"] or 0.0
    symbol_display = row["Paire"].split("->")[0] if "->" in row["Paire"] else row["Paire"].split("/")[0]

    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Opportunité actuelle</div>'
        f'<div class="simple-opp-symbol">{symbol_display}</div>'
        f'<div class="simple-opp-route">{route}</div>'
        f'<div class="simple-opp-row"><span class="k">Gain net estimé</span><span class="v">{profit_usd:+.2f} $</span></div>'
        f'<div class="simple-opp-row"><span class="k">Capital utilisé</span><span class="v">{_money(capital_usd)}</span></div>'
        f'<div class="simple-opp-row"><span class="k">Durée estimée</span><span class="v">{duration}</span></div>'
        f'<div style="margin-top:12px;font-weight:600;">{emoji} {label}</div></div>',
        unsafe_allow_html=True,
    )

    with st.expander("Pourquoi ?"):
        gross_usd = capital_usd * (row["_gross_spread_pct"] or 0) / 100
        costs_usd = gross_usd - profit_usd
        st.markdown(
            f"Le robot a détecté une différence de prix pour **{symbol_display}** ({route}).\n\n"
            f"- Gain brut : **{gross_usd:+.2f} $**\n"
            f"- Frais et coûts estimés : **{-costs_usd:+.2f} $**\n"
            f"- Gain net estimé : **{profit_usd:+.2f} $**\n\n"
            f"Le robot considère actuellement cette opportunité comme une **{label.lower()}**."
        )


def render_ignored_example(df: pd.DataFrame) -> None:
    if df.empty:
        return
    ignored = df[df["Gain net (%)"] <= 0]
    if ignored.empty:
        return
    row = ignored.iloc[0]
    capital_usd = row["_capital_usd"] or 0.0
    gross_usd = capital_usd * (row["_gross_spread_pct"] or 0) / 100
    net_usd = row["_expected_profit_usd"] if row["_expected_profit_usd"] is not None else capital_usd * (row["Gain net (%)"] or 0) / 100
    costs_usd = gross_usd - net_usd
    with st.expander(f"Pourquoi une opportunité a été ignorée récemment ({row['Paire']})"):
        st.markdown(
            f"**Opportunité ignorée** — {row['Paire']}\n\n"
            f"- Gain brut : **{gross_usd:+.2f} $**\n"
            f"- Frais et coûts estimés : **{-costs_usd:+.2f} $**\n"
            f"- Résultat net : **{net_usd:+.2f} $**\n\n"
            "Le trade aurait probablement perdu de l'argent — c'est pour ça que le robot ne l'a pas pris."
        )


def render_explainer(today: RotationReport | None) -> None:
    executed = today.completed_trades if today else 0
    winning = today.win_count if today else 0
    net_pnl = today.net_pnl_usd if today else 0.0
    funnel = data.get_opportunity_funnel_cached(hours=24.0)
    narrative = build_explainer_narrative(funnel["observed"], funnel["valid"], executed, winning, net_pnl)
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">🤖 Robot explique</div>'
        f'<div class="simple-card-sub" style="color:{INK_SECONDARY};">{narrative}</div></div>',
        unsafe_allow_html=True,
    )


def render_performance_summary() -> None:
    today = data.get_rotation_report_cached(mode=None, hours=24.0)
    week = data.get_rotation_report_cached(mode=None, hours=24.0 * 7)
    month = data.get_rotation_report_cached(mode=None, hours=24.0 * 30)

    rows = []
    for label, report in [("Aujourd'hui", today), ("7 jours", week), ("30 jours", month)]:
        if report is None or report.completed_trades == 0:
            value_html = f'<span class="v" style="color:{INK_MUTED};font-weight:500;">Pas encore assez de données</span>'
        else:
            tone_color = STATUS_GOOD if report.net_pnl_usd >= 0 else STATUS_CRITICAL
            value_html = f'<span class="v" style="color:{tone_color};">{report.net_pnl_usd:+.2f} $</span>'
        rows.append(f'<div class="simple-perf-row"><span class="k">{label}</span>{value_html}</div>')

    st.markdown(
        '<div class="simple-card-label" style="margin-top:6px;">Performance</div>'
        f'<div class="simple-card">{"".join(rows)}</div>',
        unsafe_allow_html=True,
    )


def render_equity_chart(hours: float = 24.0) -> None:
    points = data.get_equity_curve_cached(hours=hours)
    st.markdown('<div class="simple-card-label" style="margin-top:6px;">Évolution du capital</div>', unsafe_allow_html=True)
    if len(points) < 2:
        st.info("Pas encore assez de données pour tracer l'évolution du capital.")
        return
    fig = go.Figure(
        go.Scatter(
            x=[p.at for p in points],
            y=[p.capital_usd for p in points],
            mode="lines",
            line=dict(color=SEQUENTIAL_BLUE, width=2.5),
            fill="tozeroy",
            fillcolor="rgba(57,135,229,0.08)",
        )
    )
    fig.update_yaxes(title=None)
    fig.update_xaxes(title=None)
    st.plotly_chart(style_fig(fig, height=260), use_container_width=True)


def render_accueil() -> None:
    df = data.get_opportunities_cached()
    capital = data.get_simple_capital_cached()
    today_report = data.get_rotation_report_cached(mode=None, hours=24.0)
    daily = data.get_daily_summary_cached()
    utilization = data.get_capital_utilization_cached()
    positions = data.get_open_positions_cached()

    render_capital_card(capital, utilization)
    render_gain_card(capital, today_report)
    render_trades_rotation_grid(today_report, utilization)
    render_positions_card(positions)
    render_state_card(daily)
    render_opportunity_card(df)
    render_ignored_example(df)
    render_explainer(today_report)
    render_performance_summary()
    if st.button("Voir plus →", key="perf_see_more"):
        st.session_state.simple_page = "performance"
        st.rerun()
    render_equity_chart()


# --- Trades page (spec section 16) ---


def render_trades_page() -> None:
    st.markdown('<div style="font-size:1.4rem;font-weight:700;margin:6px 0 14px 0;">Trades</div>', unsafe_allow_html=True)
    trades = data.get_recent_trades_cached(limit=100)
    if not trades:
        st.info("Aucun trade pour l'instant.")
        return

    for t in trades:
        time_label = t.executed_at.strftime("%H:%M")
        asset = t.symbol.split("->")[0].split("/")[0]
        status_label = "Gagné" if t.won else "Perdu"
        with st.expander(f"{time_label} · {asset} · {t.net_profit_usd:+.2f} $ · {status_label}"):
            detail_lines = [
                f"- Stratégie : **{STRATEGY_LABELS_SIMPLE.get(t.strategy, t.strategy)}**",
                f"- Capital utilisé : **{_money(t.capital_usd)}**",
                f"- Gain brut : **{t.gross_profit_usd:+.2f} $**",
                f"- Frais : **{-t.fees_usd:+.2f} $**",
                f"- Gain net : **{t.net_profit_usd:+.2f} $**",
            ]
            if t.holding_period_seconds is not None:
                detail_lines.append(f"- Durée : **{format_duration(t.holding_period_seconds)}**")
            st.markdown("\n".join(detail_lines))


# --- Performance page (spec sections 13-14, 42-43) ---


def render_opportunity_funnel() -> None:
    """Continuous Execution spec, sections 42-43, urgent audit item 6 — how
    many raw ticks came in, how many were genuinely distinct, how many
    cleared fees, how many were actually attempted, and how many filled."""
    funnel = data.get_opportunity_funnel_cached(hours=24.0)
    breakdown = data.get_trade_status_breakdown_cached(hours=24.0)
    attempts = (breakdown.closed + breakdown.open + breakdown.failed) if breakdown else 0
    filled = (breakdown.closed + breakdown.open) if breakdown else 0
    winning = breakdown.winning if breakdown else 0

    st.markdown('<div class="simple-card-label" style="margin-top:6px;">Entonnoir des opportunités (24h)</div>', unsafe_allow_html=True)
    stages = [
        ("Observations de marché", funnel["observed"]),
        ("Opportunités uniques", funnel["unique"]),
        ("Opportunités valides", funnel["valid"]),
        ("Tentatives d'exécution", attempts),
        ("Trades exécutés", filled),
        ("Gagnants", winning),
    ]
    rows = "".join(
        f'<div class="simple-perf-row"><span class="k">{label}</span><span class="v">{value:,}</span></div>'.replace(",", " ")
        for label, value in stages
    )
    st.markdown(f'<div class="simple-card">{rows}</div>', unsafe_allow_html=True)

    if funnel["rejections"]:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Pourquoi les opportunités sont rejetées ?</div>', unsafe_allow_html=True)
        total_rejections = sum(count for _, count in funnel["rejections"])
        reason_rows = []
        for reason, count in funnel["rejections"]:
            label = REJECTION_REASON_LABELS.get(reason, reason)
            share = count / total_rejections * 100 if total_rejections else 0.0
            reason_rows.append(
                f'<div class="simple-perf-row"><span class="k">{label}</span>'
                f'<span class="v" style="color:{INK_PRIMARY};">{count:,} <span style="color:{INK_MUTED};font-weight:500;">({share:.0f} %)</span></span></div>'.replace(",", " ")
            )
        st.markdown(f'<div class="simple-card">{"".join(reason_rows)}</div>', unsafe_allow_html=True)


def render_trade_status_breakdown() -> None:
    """Continuous Execution spec, urgent audit item 4 — Closed / Winning /
    Losing / Open / Failed shown separately, so an implausible win rate (or
    a suspicious 0-loss streak) is visible as the real number it is,
    instead of hiding inside one ambiguous "trades" total."""
    breakdown = data.get_trade_status_breakdown_cached(hours=24.0)
    if breakdown is None:
        return
    st.markdown('<div class="simple-card-label" style="margin-top:14px;">Trades par statut (24h)</div>', unsafe_allow_html=True)
    rows = [
        ("Clôturés", breakdown.closed, INK_PRIMARY),
        ("Gagnants", breakdown.winning, STATUS_GOOD),
        ("Perdants", breakdown.losing, STATUS_CRITICAL),
        ("En cours", breakdown.open, INK_PRIMARY),
        ("Échoués (jamais exécutés)", breakdown.failed, INK_MUTED),
    ]
    html_rows = "".join(
        f'<div class="simple-perf-row"><span class="k">{label}</span><span class="v" style="color:{color};">{value:,}</span></div>'.replace(",", " ")
        for label, value, color in rows
    )
    st.markdown(f'<div class="simple-card">{html_rows}</div>', unsafe_allow_html=True)


def render_performance_page() -> None:
    st.markdown('<div style="font-size:1.4rem;font-weight:700;margin:6px 0 14px 0;">Performance</div>', unsafe_allow_html=True)
    render_performance_summary()
    render_equity_chart(hours=24.0 * 7)
    render_trade_status_breakdown()
    render_opportunity_funnel()


# --- Paramètres page (spec sections 15, 27) ---


def render_parametres_page() -> None:
    st.markdown('<div style="font-size:1.4rem;font-weight:700;margin:6px 0 14px 0;">Paramètres</div>', unsafe_allow_html=True)
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Mode</div>'
        '<div class="simple-card-sub"><span class="simple-sim-badge">MODE SIMULATION</span><br>'
        "Aucun argent réel n'est engagé — tout est simulé pour l'instant.</div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Portefeuille de référence</div>'
        f'<div class="simple-card-sub">{data.ROTATION_REFERENCE_PORTFOLIO} — c\'est le capital virtuel affiché sur la page d\'accueil.</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Plateformes surveillées</div>'
        f'<div class="simple-card-sub">{", ".join(e.capitalize() for e in PRIORITY_EXCHANGES)}</div></div>',
        unsafe_allow_html=True,
    )
    if st.button("Passer en Mode Expert →", key="settings_expert"):
        st.session_state.mode = "expert"
        st.rerun()


def render_simple_mode() -> None:
    page = st.session_state.get("simple_page", "accueil")
    render_header(page)
    if page == "trades":
        render_trades_page()
    elif page == "performance":
        render_performance_page()
    elif page == "parametres":
        render_parametres_page()
    else:
        render_accueil()
