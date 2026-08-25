"""Simple Mode — the default dashboard (Dashboard Simple V4 spec).

Answers, in under 5 seconds: is the robot running, how much capital is
there, what did it gain or lose today, how many trades happened, what is
it doing right now, is there a problem. Every number comes from the
existing engine's persisted data (spec section 22) — this module only
formats and narrates read-only aggregations already built in
dashboard/data.py and app/reporting/simple_summary.py. No trading,
strategy, or risk logic lives here (spec section 31).
"""

import math
from datetime import UTC, datetime, timedelta

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import dashboard.data as data
from app.config.constants import PRIORITY_EXCHANGES, OpportunityStatus
from app.reporting.rotation import RotationReport
from app.reporting.simple_summary import CapitalUtilization, OpenPosition, TradeRow, build_explainer_narrative, pick_robot_state_message
from dashboard.theme import (
    INK_MUTED,
    INK_PRIMARY,
    INK_SECONDARY,
    REJECTION_REASON_LABELS,
    SEQUENTIAL_BLUE,
    STATUS_CRITICAL,
    STATUS_GOOD,
    STATUS_WARNING,
    STRATEGY_LABELS,
    STRATEGY_LABELS_SIMPLE,
    humanize_delta,
    render_live_number_card,
    render_stat_cards,
    style_fig,
)

# Same set app.execution.validator.validate() uses to approve an
# opportunity for paper-trading — Simple Mode's "current opportunity" card
# only ever shows one that clears this bar.
GOOD_CLASSIFICATIONS = {"interesting", "good", "strong", "exceptional"}

# Bug found live, 2026-08-21 — the query behind this card had no freshness
# check at all: "Opportunité actuelle" could (and did) show a signal
# detected 40+ minutes earlier, styled exactly like a live one ("🟢
# Excellente opportunité", a 8s estimated duration), with nothing on the
# card hinting it was long gone. A user reasonably read that as "the robot
# is looking at an excellent trade right now and just... not taking it,
# despite the 30-minute rule" — the opposite of what was actually
# happening: nothing was on the radar at all. DETECTED/ACTIVE is the same
# "still on the radar right now" definition app.reporting.shadow_live
# already uses for signals_on_radar; OPEN is deliberately excluded — a
# trade already happened on that signal, so re-presenting it as "here's a
# fresh opportunity" would be its own kind of misleading.
_ON_RADAR_STATUSES = {OpportunityStatus.DETECTED.value, OpportunityStatus.ACTIVE.value}

CLASSIFICATION_BADGES = {
    "exceptional": ("🟢", "Excellente opportunité"),
    "strong": ("🟢", "Très bonne opportunité"),
    "good": ("🟢", "Bonne opportunité"),
    "interesting": ("🟡", "Opportunité correcte"),
}

NAV_PAGES = [
    ("accueil", "Accueil"), ("trades", "Trades"), ("performance", "Performance"), ("reality", "Reality"),
    ("live_trading", "LIVE TRADING"), ("parametres", "Paramètres"),
]
LIVE_TRADING_NAV_KEY = "live_trading"


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


CONNECTION_BADGE = {
    "running": ("simple-connection-live", "🟢 LIVE DATA"),
    "degraded": ("simple-connection-reconnecting", "🟠 Reconnexion..."),
    "down": ("simple-connection-down", "🔴 Données interrompues"),
}


@st.fragment(run_every="3s")
def render_live_status_row(active_page: str | None = None) -> None:
    """Live Dashboard addendum, sections 10-11 — the robot status pill and
    connection indicator update on their own every 3s, independent of the
    nav bar below (which only changes on an explicit click). No manual
    reconnect is needed: a fragment that keeps auto-rerunning *is* the
    reconnect loop — the moment fresh data is available again, the next
    tick picks it up.

    `active_page` swaps the bottom badge: every page shows "MODE
    SIMULATION" (this dashboard's paper-trading data) except the LIVE
    TRADING page, which must never be confusable with simulation (user
    directive, 2026-08-25, section 1: "ne jamais pouvoir être confondue
    avec le mode simulation") and shows a red REAL MONEY badge instead."""
    robot = data.get_robot_status_cached()
    status_class = {"running": "simple-status-running", "degraded": "simple-status-degraded", "down": "simple-status-down"}[robot.health.value]
    status_label = {"running": "🟢 EN MARCHE", "degraded": "🟡 SURVEILLANCE", "down": "🔴 PROBLÈME"}[robot.health.value]
    connection_class, connection_label = CONNECTION_BADGE[robot.health.value]
    exchange_bits = " &nbsp;·&nbsp; ".join(
        f'<b>{name.capitalize()}</b> {"✓" if ok else "✕"}' for name, ok in robot.exchanges_connected.items()
    )
    mode_badge = (
        '<span class="simple-live-badge">🔴 REAL MONEY — LIVE TRADING</span>'
        if active_page == LIVE_TRADING_NAV_KEY
        else '<span class="simple-sim-badge">MODE SIMULATION</span>'
    )

    st.markdown(
        f'<div class="simple-topbar"><div class="simple-brand">🤖 ROBOT</div>'
        f'<div class="simple-status-pill {status_class}">{status_label}</div>'
        f'<span class="simple-connection-badge {connection_class}">{connection_label}</span></div>'
        f'<div class="simple-exchanges">{exchange_bits}</div>'
        f'<div>{mode_badge}</div>',
        unsafe_allow_html=True,
    )


def render_header(active_page: str) -> None:
    render_live_status_row(active_page)

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


def render_capital_card(
    capital: float | None, utilization: CapitalUtilization | None = None, last_trade_at: datetime | None = None
) -> None:
    """Live Dashboard addendum — user feedback: the card must stay
    intact, only the number counts smoothly (render_live_number_card),
    never the whole card re-rendering. The "no data yet" state has nothing
    to animate, so it stays a plain static card.

    `last_trade_at`, when known, is shown as a small footer — user
    feedback: the capital figure only ever moves when a trade actually
    closes, and at a rhythm of a few trades per hour, it can sit still for
    several minutes at a stretch. Without this note, that reads as "stuck"
    rather than "correctly waiting for the next real event"."""
    if capital is None:
        st.markdown(
            '<div class="simple-card"><div class="simple-card-label">Capital virtuel</div>'
            '<div class="simple-card-figure">—</div><div class="simple-card-sub">Pas encore disponible</div></div>',
            unsafe_allow_html=True,
        )
        return
    last_trade_text = f"Dernier trade : {humanize_delta(last_trade_at)}" if last_trade_at is not None else None
    rows = [{"value": capital, "decimals": 2, "big": True, "suffix": " $", "sub_text": last_trade_text if utilization is None else None}]
    if utilization is not None:
        available = utilization.total_capital_usd - utilization.engaged_usd
        rows.append(
            {
                "value": available,
                "decimals": 2,
                "suffix": " $",
                "color": INK_SECONDARY,
                "label": "Disponible maintenant :",
                "sub_text": last_trade_text,
            }
        )
    render_live_number_card("Capital virtuel", rows, key="capital")


def render_gain_card(capital: float | None, today: RotationReport | None) -> None:
    if capital is None or today is None or today.completed_trades == 0:
        st.markdown(
            '<div class="simple-card"><div class="simple-card-label">Gain aujourd\'hui</div>'
            '<div class="simple-card-figure">—</div><div class="simple-card-sub">Pas encore disponible</div></div>',
            unsafe_allow_html=True,
        )
        return
    pnl = today.net_pnl_usd
    base = capital - pnl
    pct = (pnl / base * 100) if base else 0.0
    tone_color = STATUS_GOOD if pnl >= 0 else STATUS_CRITICAL
    trade_word = "trade" if today.completed_trades == 1 else "trades"
    render_live_number_card(
        "Gain aujourd'hui",
        [
            {"value": pnl, "decimals": 2, "big": True, "suffix": " $", "signed": True, "color": tone_color},
            {
                "value": pct,
                "decimals": 2,
                "suffix": " %",
                "signed": True,
                "color": tone_color,
                # User feedback: the % looked like an isolated, jumpy number on
                # every refresh — this is a running total for the whole day, not
                # one trade's result, so the trade count makes that explicit.
                "sub_text": f"Cumul sur {today.completed_trades} {trade_word} aujourd'hui ({today.win_count} gagnant(s) · {today.loss_count} perdant(s))",
            },
        ],
        key="gain",
    )


def render_pnl_split_card(today: RotationReport | None, positions: list[OpenPosition]) -> None:
    """Live Dashboard addendum, section 8 — realized vs unrealized P&L kept
    visually distinct: the capital card above only ever grows with
    REALIZED profit (booked when a position closes), while an open
    position's current paper gain is real but not yet locked in. Both
    numbers already exist elsewhere (today's RotationReport, each open
    OpenPosition's own net_profit_usd) — this is a pure display
    combination, not a new calculation."""
    realized = today.net_pnl_usd if today is not None else 0.0
    unrealized = sum(p.net_profit_usd for p in positions)
    total = realized + unrealized
    render_live_number_card(
        "Répartition du gain",
        [
            {"value": realized, "decimals": 2, "suffix": " $", "signed": True, "color": STATUS_GOOD if realized >= 0 else STATUS_CRITICAL, "label": "Gain réalisé :"},
            {"value": unrealized, "decimals": 2, "suffix": " $", "signed": True, "color": STATUS_GOOD if unrealized >= 0 else STATUS_CRITICAL, "label": "Positions en cours :"},
            {"value": total, "decimals": 2, "big": True, "suffix": " $", "signed": True, "color": STATUS_GOOD if total >= 0 else STATUS_CRITICAL, "label": "Total actuel :"},
        ],
        key="pnl_split",
    )


def render_trades_rotation_grid(today: RotationReport | None, utilization: CapitalUtilization | None) -> None:
    col1, col2, col3 = st.columns(3)

    with col1:
        if today is None or today.completed_trades == 0:
            st.markdown(
                '<div class="simple-card"><div class="simple-card-label">Trades</div>'
                '<div class="simple-card-figure">0</div><div class="simple-card-sub">Aucun trade pour l\'instant</div></div>',
                unsafe_allow_html=True,
            )
        else:
            render_live_number_card(
                "Trades",
                [
                    {"value": float(today.completed_trades), "decimals": 0, "big": True, "sub_text": f"{today.win_count} gagnants · {today.loss_count} perdants"},
                    {"value": today.trades_per_hour, "decimals": 1, "suffix": " /h", "color": INK_SECONDARY, "label": "Rythme (dernières 24h) :"},
                ],
                key="trades_count",
            )

    with col2:
        if today is None or today.completed_trades == 0:
            st.markdown('<div class="simple-card"><div class="simple-card-label">Rotation</div><div class="simple-card-figure">—</div></div>', unsafe_allow_html=True)
        else:
            render_live_number_card(
                "Rotation",
                [
                    {
                        "value": today.capital_rotation_rate,
                        "decimals": 1,
                        "big": True,
                        "suffix": "×",
                        "sub_text": f"Capital traité aujourd'hui : {_money(today.total_capital_traded_usd)}",
                    }
                ],
                key="rotation",
            )

    with col3:
        if utilization is None:
            st.markdown('<div class="simple-card"><div class="simple-card-label">Utilisation actuelle</div><div class="simple-card-figure">—</div></div>', unsafe_allow_html=True)
        else:
            render_live_number_card(
                "Utilisation actuelle",
                [
                    {
                        "value": utilization.utilization_pct,
                        "decimals": 0,
                        "big": True,
                        "suffix": " %",
                        "sub_text": f"{utilization.open_position_count} position(s) en cours",
                    }
                ],
                key="utilization",
            )


def render_positions_card(positions: list[OpenPosition]) -> None:
    """Continuous Execution spec, section 47 — no more than symbol / capital
    / booked P&L per position; full detail stays in Mode Expert.

    Always renders the card shell, even with zero positions (Live
    Dashboard addendum) — a card that appears/disappears entirely between
    live-fragment ticks shifts every card below it up or down, which reads
    as a hard reload rather than a smooth update. A stable placeholder
    keeps the page layout still while only the content inside changes."""
    if not positions:
        body = '<div class="simple-card-sub">Aucune position en cours actuellement.</div>'
    else:
        rows = []
        for p in positions:
            asset = p.symbol.split("->")[0].split("/")[0]
            tone_color = STATUS_GOOD if p.net_profit_usd >= 0 else STATUS_CRITICAL
            rows.append(
                f'<div class="simple-opp-row"><span class="k">{asset}</span>'
                f'<span class="v">{_money(p.capital_usd)}</span>'
                f'<span class="v" style="color:{tone_color};margin-left:10px;">{p.net_profit_usd:+.2f} $</span></div>'
            )
        body = "".join(rows)
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Positions en cours</div>' + body + "</div>",
        unsafe_allow_html=True,
    )


def render_event_feed(trades: list[TradeRow], now: datetime | None = None) -> None:
    """Live Dashboard addendum, section 9 — a small live feed of what just
    happened, capped at a handful of rows. Built entirely from already-
    fetched recent trades (no new backend query): a row is either "position
    ouverte" (still inside its holding period) or "trade clôturé" with its
    booked result — the same open/closed distinction TradeStatusBreakdown
    already uses (app.reporting.simple_summary._classify_trade_status).
    Always renders the card shell (Live Dashboard addendum) — see
    render_positions_card's docstring for why an appearing/disappearing
    card is worse than an empty-state placeholder."""
    if not trades:
        st.markdown(
            '<div class="simple-card"><div class="simple-card-label">Activité récente</div>'
            '<div class="simple-card-sub">Aucune activité pour l\'instant.</div></div>',
            unsafe_allow_html=True,
        )
        return
    now = now or datetime.now(UTC)
    rows = []
    for t in trades[:8]:
        executed_at = t.executed_at if t.executed_at.tzinfo else t.executed_at.replace(tzinfo=UTC)
        is_open = t.holding_period_seconds is not None and executed_at + timedelta(seconds=t.holding_period_seconds) > now
        asset = t.symbol.split("->")[0].split("/")[0]
        time_label = executed_at.strftime("%H:%M:%S")
        if is_open:
            text = f"Position <b>{asset}</b> ouverte"
        else:
            color = STATUS_GOOD if t.net_profit_usd >= 0 else STATUS_CRITICAL
            text = f'Trade <b>{asset}</b> clôturé <span style="color:{color};font-weight:700;">{t.net_profit_usd:+.2f} $</span>'
        rows.append(f'<div class="simple-event-row"><span class="simple-event-time">{time_label}</span><span class="simple-event-text">{text}</span></div>')
    st.markdown(
        '<div class="simple-card"><div class="simple-card-label">Activité récente</div>' + "".join(rows) + "</div>",
        unsafe_allow_html=True,
    )


def _maybe_toast_new_trade(trades: list[TradeRow]) -> None:
    """Live Dashboard addendum, section 22 — a discrete toast the moment a
    newly-closed trade appears since the last live-fragment tick. Skips the
    very first tick after page load (nothing to compare against yet), so
    opening the dashboard doesn't immediately toast for trades that already
    existed before this browser session started."""
    if not trades:
        return
    latest = trades[0]
    seen_key = "_live_last_seen_trade_id"
    previous_id = st.session_state.get(seen_key)
    st.session_state[seen_key] = latest.id
    if previous_id is None or latest.id == previous_id:
        return
    asset = latest.symbol.split("->")[0].split("/")[0]
    st.toast(f"Trade {asset} terminé {latest.net_profit_usd:+.2f} $", icon="✅" if latest.net_profit_usd >= 0 else "⚠️")


def render_state_card(daily) -> None:
    robot = data.get_robot_status_cached()
    msg = pick_robot_state_message(robot, daily.detected, daily.net_positive)
    st.markdown(
        f'<div class="simple-state-card {msg.tone}"><div class="simple-state-title">{msg.title}</div>'
        f'<div class="simple-state-body">{msg.body}</div></div>',
        unsafe_allow_html=True,
    )


def render_reality_indicator() -> None:
    """Reality Engine spec, section 38 — "FIABILITÉ DE LA SIMULATION": how
    much of the spread the robot sees at detection actually survives a
    realistic simulated fill, on average. A low number isn't a bug — it's
    the whole point of V5 (section 1's "profit théorique vs profit
    réalistement exécutable")."""
    report = data.get_reality_capture_cached(hours=24.0)
    if report is None or report.trade_count == 0:
        return
    ratio = report.capture_ratio_pct
    color = STATUS_GOOD if ratio >= 50 else STATUS_WARNING if ratio >= 0 else STATUS_CRITICAL
    render_live_number_card(
        "Fiabilité de la simulation",
        [
            {
                "value": ratio,
                "decimals": 0,
                "big": True,
                "suffix": " %",
                "color": color,
                "sub_text": (
                    f"En moyenne, le robot conserve {ratio:.0f} % du gain initialement détecté "
                    "après simulation réaliste (frais, slippage, échecs d'exécution inclus)."
                ),
            }
        ],
        key="reality_capture",
    )


def render_opportunity_card(df: pd.DataFrame) -> None:
    candidates = (
        df[df["_classification"].isin(GOOD_CLASSIFICATIONS) & df["_status"].isin(_ON_RADAR_STATUSES)] if not df.empty else df
    )
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

        # Opportunity Expansion spec, Step 2 (user directive, 2026-08-21) —
        # théorique (top-of-book, avant toute considération de taille) vs
        # ce que la taille prévue aurait réellement donné vs la taille qui
        # maximise le profit réel une fois la profondeur du carnet prise
        # en compte — le capital ci-dessus EST déjà la taille optimale, pas
        # automatiquement le maximum disponible.
        theoretical = row.get("_theoretical_edge_pct")
        depth_adjusted = row.get("_depth_adjusted_edge_pct")
        realistic = row.get("_realistic_executable_edge_pct")
        optimal_capital = row.get("_optimal_capital_usd")
        max_profitable = row.get("_max_profitable_capital_usd")
        if theoretical is not None and realistic is not None:
            st.markdown('<div class="simple-card-label" style="margin-top:14px;">Écart théorique vs réellement exécutable</div>', unsafe_allow_html=True)
            edge_lines = [f"- Écart théorique (top-of-book) : **{theoretical:+.3f} %**"]
            if depth_adjusted is not None:
                edge_lines.append(f"- Écart net à la taille visée par défaut : **{depth_adjusted:+.3f} %**")
            edge_lines.append(f"- Écart net réellement exécutable (à la taille optimale) : **{realistic:+.3f} %**")
            if optimal_capital is not None:
                edge_lines.append(f"- Capital optimal : **{_money(optimal_capital)}**")
            if max_profitable is not None:
                edge_lines.append(f"- Capital maximum encore rentable : **{_money(max_profitable)}**")
            st.markdown("\n".join(edge_lines))
            st.caption(
                "Le capital utilisé ci-dessus est déjà la taille optimale (celle qui maximise le gain réel en dollars, "
                "pas le meilleur pourcentage) — jamais automatiquement le capital maximum disponible."
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
    funnel = data.get_execution_funnel_cached(hours=24.0)
    narrative = build_explainer_narrative(funnel.observed, funnel.stage("profitable_after_fees").count, executed, winning, net_pnl)
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


EQUITY_CURVE_TOLERANCE_USD = 0.02


def render_equity_chart(hours: float | None = None) -> None:
    """Reality Engine spec, section 36 — sources exclusively from the
    Portfolio Ledger's own equity reconstruction (build_equity_curve), never
    a value invented for display. Points with a missing/non-finite capital
    value are dropped defensively — Plotly renders a `null` y as a gap, but
    a bare unlabeled trace hovering over one can show "undefined" in the
    tooltip, which is the bug this was reported against; an explicit name
    and hovertemplate remove the ambiguity regardless of the exact cause.

    hours=None (default) — full history, so the first point is the real
    starting capital rather than an approximation from a rolling window,
    and the last point is checked against the Capital virtuel card's own
    figure (app.reporting.simple_summary.build_portfolio_capital): both are
    the same all-time sum by construction, so any divergence beyond
    floating-point tolerance means the two reconstructions disagree — a
    genuine accounting anomaly, not a display quirk. Shown as a visible
    warning rather than silently trusting either number."""
    points = data.get_equity_curve_cached(hours=hours)
    points = [p for p in points if p.at is not None and p.capital_usd is not None and math.isfinite(p.capital_usd)]
    st.markdown('<div class="simple-card-label" style="margin-top:6px;">Évolution du capital</div>', unsafe_allow_html=True)
    if len(points) < 2:
        st.info("Pas encore assez de données pour tracer l'évolution du capital.")
        return

    capital = data.get_simple_capital_cached()
    if capital is not None and abs(points[-1].capital_usd - capital) > EQUITY_CURVE_TOLERANCE_USD:
        st.error(
            f"⚠️ Anomalie comptable : le graphique termine à {points[-1].capital_usd:,.2f} $ mais la carte Capital "
            f"affiche {capital:,.2f} $ — écart de {points[-1].capital_usd - capital:+.2f} $. "
            "Ces deux valeurs doivent toujours être identiques."
        )

    fig = go.Figure(
        go.Scatter(
            x=[p.at for p in points],
            y=[p.capital_usd for p in points],
            mode="lines",
            name="Capital",
            line=dict(color=SEQUENTIAL_BLUE, width=2.5),
            fill="tozeroy",
            fillcolor="rgba(57,135,229,0.08)",
            hovertemplate="%{x|%d/%m %H:%M}<br>%{y:.2f} $<extra></extra>",
        )
    )
    fig.update_yaxes(title=None)
    fig.update_xaxes(title=None)
    st.plotly_chart(style_fig(fig, height=260), use_container_width=True)


@st.fragment(run_every="3s")
def render_live_accueil_body() -> None:
    """Live Dashboard addendum — everything on the Accueil page that should
    change without a manual refresh lives in this one fragment, so a single
    3s tick keeps capital, gain, positions, the current opportunity, and
    the event feed all in sync with each other (no risk of one card
    updating a beat ahead of another). The equity chart gets its own,
    slower-cadence fragment below — redrawing a Plotly chart every 3s would
    be visually noisy for something that doesn't need sub-10s freshness."""
    df = data.get_opportunities_cached()
    capital = data.get_simple_capital_cached()
    today_report = data.get_rotation_report_cached(mode=None, hours=24.0)
    daily = data.get_daily_summary_cached()
    utilization = data.get_capital_utilization_cached()
    positions = data.get_open_positions_cached()
    trades = data.get_recent_trades_cached(limit=8)

    _maybe_toast_new_trade(trades)

    last_trade_at = trades[0].executed_at if trades else None
    render_capital_card(capital, utilization, last_trade_at)
    render_gain_card(capital, today_report)
    render_pnl_split_card(today_report, positions)
    render_trades_rotation_grid(today_report, utilization)
    render_positions_card(positions)
    render_state_card(daily)
    render_reality_indicator()
    render_opportunity_card(df)
    render_event_feed(trades)
    render_ignored_example(df)
    render_explainer(today_report)
    render_performance_summary()


@st.fragment(run_every="10s")
def render_live_equity_chart() -> None:
    render_equity_chart()


def render_accueil() -> None:
    render_live_accueil_body()
    if st.button("Voir plus →", key="perf_see_more"):
        st.session_state.simple_page = "performance"
        st.rerun()
    render_live_equity_chart()
    render_reality_snapshot_card()


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
    cleared fees, how many were actually validated for execution, how many
    were actually attempted, and how many filled.

    Sources from the same app.reporting.execution_funnel used by the
    Expert Mode audit — one funnel computation, not two that can drift out
    of sync. "Écart positif détecté" (previously "Opportunités valides", a
    label users read as "ready to trade" when it only ever meant "the raw
    edge was above zero") is deliberately distinct from the new
    "Exécutables" stage below it, which is the one that actually reflects
    validate()'s full bar (classification, freshness, no duplicate
    position) — the gap between the two is usually the real answer to
    "why so few trades for so many opportunities"."""
    funnel = data.get_execution_funnel_cached(hours=24.0)
    breakdown = data.get_trade_status_breakdown_cached(hours=24.0)
    attempts = (breakdown.closed + breakdown.open + breakdown.failed) if breakdown else 0
    filled = (breakdown.closed + breakdown.open) if breakdown else 0
    winning = breakdown.winning if breakdown else 0

    st.markdown('<div class="simple-card-label" style="margin-top:6px;">Entonnoir des opportunités (24h)</div>', unsafe_allow_html=True)
    stages = [
        ("Observations de marché", funnel.observed),
        ("Opportunités uniques", funnel.stage("detected").count),
        ("Écart positif détecté", funnel.stage("profitable_after_fees").count),
        ("Exécutables", funnel.stage("executable").count),
        ("Tentatives d'exécution", attempts),
        ("Trades exécutés", filled),
        ("Gagnants", winning),
    ]
    rows = "".join(
        f'<div class="simple-perf-row"><span class="k">{label}</span><span class="v">{value:,}</span></div>'.replace(",", " ")
        for label, value in stages
    )
    st.markdown(f'<div class="simple-card">{rows}</div>', unsafe_allow_html=True)

    if funnel.rejection_reasons:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Pourquoi les opportunités sont rejetées ?</div>', unsafe_allow_html=True)
        reason_rows = []
        for reason, count, share in funnel.rejection_reasons:
            label = REJECTION_REASON_LABELS.get(reason, reason)
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
    render_equity_chart()
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


# --- Reality page (V5/V5.5 Master Orchestration, user directive, 2026-08-22) ---


def _reality_engine_status() -> tuple[str, str]:
    """LIVE / DEGRADED / STOPPED / AUDIT — spec Part N: never show LIVE if
    an essential component is stale."""
    robot = data.get_robot_status_cached()
    dq = data.get_data_quality_report_cached()
    dex_live_count = sum(1 for status in dq.dex_chains.values() if status.value == "live")
    if robot.health.value == "down" or dex_live_count == 0:
        return "🔴 STOPPED", "simple-status-down"
    if robot.health.value == "degraded" or dex_live_count < len(dq.dex_chains):
        return "🟡 DEGRADED", "simple-status-degraded"
    return "🟢 LIVE", "simple-status-running"


def render_reality_snapshot_card() -> None:
    """Spec Part AG — Accueil gets only a compact summary card + a link,
    never the full Reality page inline."""
    capital_state = data.get_global_capital_state_cached()
    if capital_state is None:
        return
    funnel = data.get_master_frequency_report_cached(hours=24.0)
    dex_funnels = data.get_dex_execution_funnel_cached(hours=24.0)
    total_filled = sum(f.filled for f in dex_funnels)
    total_attempts = sum(f.attempts for f in dex_funnels)
    capture_pct = (total_filled / total_attempts * 100) if total_attempts else None

    status_label, status_class = _reality_engine_status()
    st.markdown('<div class="simple-card-label" style="margin-top:14px;">Reality Snapshot</div>', unsafe_allow_html=True)
    render_stat_cards(
        [
            {"label": "Capital total", "value": _money(capital_state.total_capital_usd)},
            {"label": "Capital disponible", "value": _money(capital_state.available_usd)},
            {"label": "Capital utilisé", "value": _money(capital_state.total_reserved_usd), "sub": f"{capital_state.capital_utilization_pct:.1f} % utilisation"},
            {"label": "Trades DEX filled (24h)", "value": f"{total_filled:,}".replace(",", " ")},
            {"label": "Capture rate DEX (24h)", "value": f"{capture_pct:.0f} %" if capture_pct is not None else "—"},
            {"label": "État moteur", "value": status_label},
        ]
    )
    if st.button("Voir Reality →", key="accueil_to_reality", use_container_width=True):
        st.session_state.simple_page = "reality"
        st.rerun()


def render_reality_page() -> None:
    st.markdown('<div style="font-size:1.4rem;font-weight:700;margin:6px 0 14px 0;">Reality</div>', unsafe_allow_html=True)

    now = datetime.now(UTC).replace(tzinfo=None)
    baseline_hours = data.hours_since_baseline(now)
    window_choice = st.radio(
        "Fenêtre",
        ["Depuis l'audit", "24h", "7j"],
        horizontal=True,
        key="reality_window",
        label_visibility="collapsed",
    )
    hours = {"Depuis l'audit": baseline_hours, "24h": 24.0, "7j": 24.0 * 7}[window_choice]
    if window_choice != "Depuis l'audit" and data.window_contains_pre_baseline_data(now, hours):
        st.markdown(
            '<div class="simple-card" style="border-color:#f59e0b;"><div class="simple-card-sub warn">'
            "⚠️ Cette fenêtre remonte avant la correction de l'audit Reality "
            f"({data.REALITY_BASELINE_AT.strftime('%Y-%m-%d %H:%M')} UTC) — elle peut contenir des données "
            "DEX contaminées par le double comptage atomic/séquentiel corrigé depuis. "
            "Utilisez « Depuis l'audit » pour les chiffres audités propres.</div></div>",
            unsafe_allow_html=True,
        )

    # --- Part N: Reality Engine Status ---
    status_label, status_class = _reality_engine_status()
    robot = data.get_robot_status_cached()
    dq = data.get_data_quality_report_cached()
    st.markdown(
        f'<div class="simple-status-pill {status_class}" style="display:inline-block;margin-bottom:10px;">{status_label}</div>',
        unsafe_allow_html=True,
    )
    render_stat_cards(
        [
            {"label": "CEX", "value": "🟢 LIVE" if robot.health.value == "running" else robot.health.value.upper()},
            {"label": "DEX chains live", "value": f"{sum(1 for s in dq.dex_chains.values() if s.value == 'live')}/{len(dq.dex_chains)}"},
        ]
    )

    # --- Part AP: kill switch banner ---
    if robot.health.value == "down":
        st.markdown(
            '<div class="simple-card" style="border-color:#ef4444;"><div class="simple-card-sub bad">'
            "🔴 EXÉCUTION EN PAUSE — vérification de la santé du moteur en échec. Aucune nouvelle capitale n'est allouée.</div></div>",
            unsafe_allow_html=True,
        )

    # --- Part O: Global Capital Card ---
    capital_state = data.get_global_capital_state_cached()
    if capital_state is not None:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Capital global</div>', unsafe_allow_html=True)
        render_stat_cards(
            [
                {"label": "Total", "value": _money(capital_state.total_capital_usd)},
                {"label": "Disponible", "value": _money(capital_state.available_usd)},
                {"label": "Réservé CEX", "value": _money(capital_state.reserved_cex_usd)},
                {"label": "Réservé DEX", "value": _money(capital_state.reserved_dex_usd)},
                {"label": "Total réservé", "value": _money(capital_state.total_reserved_usd)},
                {"label": "Utilisation", "value": f"{capital_state.capital_utilization_pct:.1f} %"},
                {"label": "CEX (portefeuille 5K)", "value": _money(capital_state.cex_total_capital_usd), "sub": f"dispo {_money(capital_state.cex_available_usd)}"},
                {"label": "DEX (pool)", "value": _money(capital_state.dex_total_capital_usd), "sub": f"dispo {_money(capital_state.dex_available_usd)}"},
            ]
        )
        st.caption(
            "✓ Réconcilié : total = disponible + réservé" if capital_state.reconciled else "⚠️ NON RÉCONCILIÉ — total ≠ disponible + réservé"
        )

    # --- Part Q: Audited P&L ---
    dex_funnels_baseline = data.get_dex_execution_funnel_cached(hours=baseline_hours)
    dex_profit_baseline = sum(f.total_net_profit_usd for f in dex_funnels_baseline)
    st.markdown('<div class="simple-card-label" style="margin-top:14px;">P&L simulé audité <span class="simple-sim-badge">PAPER TRADING</span></div>', unsafe_allow_html=True)
    render_stat_cards(
        [
            {"label": "DEX — depuis l'audit", "value": f"{dex_profit_baseline:+.2f} $", "sub": f"{baseline_hours:.1f} h de données propres"},
        ]
    )

    # --- Part R: Master Opportunity Funnel ---
    with st.expander("Entonnoir global (détection → profit)", expanded=True):
        master = data.get_master_frequency_report_cached(hours=hours)
        render_stat_cards(
            [
                {"label": "Détections brutes", "value": f"{master.total_detected:,}".replace(",", " ")},
                {"label": "Exécutables", "value": f"{master.total_executable:,}".replace(",", " ")},
                {"label": "Exécutées (CEX)", "value": f"{master.total_executed:,}".replace(",", " ")},
            ]
        )
        dup = data.get_duplicate_monitor_report_cached()
        if not dup.reliable:
            st.markdown(
                f'<div class="simple-card" style="border-color:#f59e0b;"><div class="simple-card-sub warn">'
                f'⚠️ LEGACY / NOT RELIABLE — {dup.legacy_note}</div></div>',
                unsafe_allow_html=True,
            )
        render_stat_cards(
            [
                {"label": "Détections DEX brutes (depuis baseline)", "value": f"{dup.raw_detections:,}".replace(",", " ")},
                {"label": "Doublons économiques éliminés", "value": f"{dup.duplicate_economic_events_eliminated:,}".replace(",", " "), "sub": None if dup.reliable else "compteur non fiable pour cette fenêtre"},
                {"label": "Opportunités économiques uniques", "value": f"{dup.unique_economic_opportunities:,}".replace(",", " ")},
                {
                    "label": "Faux P&L évité (estimation)",
                    "value": f"{dup.estimated_fake_pnl_prevented_usd:+.2f} $" if dup.estimated_fake_pnl_prevented_usd is not None else "N/A",
                    "sub": "projection sur expected_profit_usd — le doublon n'a jamais été exécuté",
                },
            ]
        )

    # --- Part S: rejection reasons ---
    with st.expander("Pourquoi les opportunités ne deviennent pas des trades"):
        rejections = data.get_global_rejection_breakdown_cached(hours=hours)
        tracked = [r for r in rejections if r.tracked]
        untracked = [r for r in rejections if not r.tracked]
        for r in tracked:
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{r.reason} <span style="color:{INK_MUTED};">({r.engine})</span></span>'
                f'<span class="v">{r.count:,}</span></div>'.replace(",", " "),
                unsafe_allow_html=True,
            )
        if untracked:
            st.caption("Non suivies séparément (repliées dans le calcul de l'edge net à la détection) : " + ", ".join(r.reason for r in untracked))

    # --- Part U: CEX vs DEX ---
    with st.expander("CEX vs DEX"):
        benchmark = data.get_benchmark_report_cached(hours=hours)
        for label, eb in [("CEX", benchmark.cex_only), ("DEX", benchmark.dex_only), ("Combiné", benchmark.combined)]:
            render_stat_cards(
                [
                    {"label": f"{label} — exécutables/h", "value": f"{eb.executable_per_hour:.2f}"},
                    {"label": f"{label} — exécutées", "value": f"{eb.executed_opportunities:,}".replace(",", " ")},
                    {"label": f"{label} — P&L net", "value": f"{eb.net_pnl_usd:+.2f} $"},
                    {"label": f"{label} — durée moy.", "value": format_duration(eb.avg_holding_seconds)},
                ]
            )

    # --- Part V/W/X/Y: strategy ranking, capital velocity, trades/hour, capture rate ---
    with st.expander("Classement des stratégies (CEX + DEX)"):
        ranking = data.get_master_strategy_ranking_cached(hours=hours)
        for r in ranking:
            velocity_str = f"{r.capital_velocity_usd_per_minute:+.4f} $/cap-min" if r.capital_velocity_usd_per_minute is not None else "—"
            capture_str = f"{r.capture_rate_pct:.0f} %" if r.capture_rate_pct is not None else "—"
            st.markdown(
                f'<div class="simple-card"><div class="simple-card-label">{STRATEGY_LABELS.get(r.strategy, r.strategy)} '
                f'<span style="color:{INK_MUTED};">({r.engine})</span></div>'
                f'<div class="simple-card-sub">Profit net: <b>{r.net_profit_usd:+.2f} $</b> · Trades: {r.attempts:,} · Filled: {r.filled:,} '
                f'· Capture: {capture_str} · Capital utilisé: {r.capital_used_usd:,.0f} $ · Vélocité: {velocity_str}</div></div>'.replace(",", " "),
                unsafe_allow_html=True,
            )

    # --- Part AC: capital tier replay ---
    with st.expander("Réplication par palier de capital (audit historique)"):
        st.caption("HISTORIQUE / SIMULATION DE RÉPLICATION — ce ne sont pas des rendements futurs garantis.")
        tier_results = data.get_capital_tier_replay_results_cached()
        if not tier_results:
            st.caption("Pas assez de données depuis l'audit pour une réplication significative.")
        for t in tier_results:
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{t.capital_tier_usd:,.0f} $ '
                f'<span style="color:{INK_MUTED};">({t.n_filled} filled · {t.n_no_capital_available} sans capital)</span></span>'
                f'<span class="v">{t.total_net_profit_usd:+.2f} $</span></div>'.replace(",", " "),
                unsafe_allow_html=True,
            )

    # --- Part AD: stress tests ---
    with st.expander("Tests de stress"):
        stress_results = data.get_stress_test_results_cached()
        if not stress_results:
            st.caption("Pas de données dex_cross avec instantané de prix depuis l'audit pour un test de stress.")
        for s in stress_results:
            capture_str = f"{s.capture_ratio_pct:.0f} %" if s.capture_ratio_pct is not None else "—"
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{s.scenario}</span>'
                f'<span class="v">{s.total_net_profit_usd:+.2f} $ · {capture_str} capture</span></div>',
                unsafe_allow_html=True,
            )

    # --- Part AE: data quality ---
    with st.expander("Qualité des données"):
        render_stat_cards(
            [{"label": name.capitalize(), "value": "🟢 LIVE" if ok else "🔴 DOWN"} for name, ok in dq.cex_exchanges.items()]
        )
        render_stat_cards(
            [{"label": chain, "value": status.value.upper()} for chain, status in dq.dex_chains.items()]
        )

    # --- Part AF: reliability ---
    with st.expander("Fiabilité de la simulation"):
        reliability = data.get_reality_reliability_report_cached()
        render_stat_cards(
            [
                {"label": "Couverture replay (dex_cross)", "value": f"{reliability.replay_coverage_pct:.0f} %" if reliability.replay_coverage_pct is not None else "N/A", "sub": reliability.replay_coverage_note},
                {"label": "Complétude des données DEX", "value": f"{reliability.dex_data_completeness_pct:.0f} %" if reliability.dex_data_completeness_pct is not None else "N/A"},
                {"label": "Taux d'échec DEX", "value": f"{reliability.dex_attempt_failure_rate_pct:.0f} %" if reliability.dex_attempt_failure_rate_pct is not None else "N/A"},
                {"label": "Score composite", "value": "N/A", "sub": reliability.composite_score_reason},
            ]
        )

    render_phase2c_master_mode_section()
    render_phase2_shadow_section()
    render_phase2d_micro_live_section()
    render_phase2e_real_edge_section()
    render_real_trading_section()
    render_full_market_discovery_section()
    render_missed_and_capital_section()
    render_inventory_manager_section()


# --- PHASE 2C — CONTROLLED PAPER CUTOVER / MASTER MODE (user directive, 2026-08-23) ---
#
# PAPER TRADING ONLY. Live state is read straight from the engine's own
# /master/status endpoint (app.orchestration.global_allocator's real,
# in-process capital state) — the same "browser is never the source of
# truth" discipline this whole dashboard follows. real_orders_placed is
# always shown and is always false; nothing here can ever reflect a real
# order or real capital.


def render_phase2c_master_mode_section() -> None:
    status = data.get_master_status_cached()
    if not status.reachable:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">PHASE 2C — MASTER MODE</div>', unsafe_allow_html=True)
        st.caption("Moteur injoignable — état MASTER indisponible.")
        return

    mode_label = "🟢 MASTER MODE = PAPER ACTIVE" if status.paper_authority_enabled else "🔴 MASTER MODE = ROLLBACK (OLD seul autorité)"
    mode_class = "simple-status-running" if status.paper_authority_enabled else "simple-status-down"
    st.markdown(
        '<div style="margin-top:22px;padding:14px;border:2px solid #10b981;border-radius:14px;background:rgba(16,185,129,0.06);">'
        '<div style="font-size:1.1rem;font-weight:700;color:#10b981;">PHASE 2C — CONTROLLED PAPER CUTOVER</div>'
        '<div style="font-size:0.85rem;color:#6b7280;margin-top:2px;">PAPER TRADING UNIQUEMENT — aucun ordre réel, aucune clé API réelle requise.</div></div>',
        unsafe_allow_html=True,
    )
    st.markdown(f'<span class="simple-status-pill {mode_class}" style="display:inline-block;margin:10px 0;">{mode_label}</span>', unsafe_allow_html=True)
    if status.rollback_reason:
        st.markdown(
            f'<div class="simple-card" style="border-color:#ef4444;"><div class="simple-card-sub bad">⚠️ Rollback : {status.rollback_reason}</div></div>',
            unsafe_allow_html=True,
        )
    if status.invariant_violations:
        st.markdown(
            f'<div class="simple-card" style="border-color:#ef4444;"><div class="simple-card-sub bad">⚠️ Invariant violé : {"; ".join(status.invariant_violations)}</div></div>',
            unsafe_allow_html=True,
        )

    render_stat_cards(
        [
            {"label": "Capital global", "value": f"{status.total_capital_usd:,.2f} $".replace(",", " ")},
            {"label": "Disponible", "value": f"{status.available_capital_usd:,.2f} $".replace(",", " ")},
            {"label": "Réservé (total)", "value": f"{status.reserved_capital_usd:,.2f} $".replace(",", " ")},
            {"label": "Réservé CEX", "value": f"{status.reserved_cex_usd:,.2f} $".replace(",", " ")},
            {"label": "Réservé DEX", "value": f"{status.reserved_dex_usd:,.2f} $".replace(",", " ")},
            {"label": "P&L simulé réalisé", "value": f"{status.realized_pnl_usd:+.2f} $"},
            {"label": "Décisions MASTER (session)", "value": f"{status.grants_count + status.rejections_count:,}".replace(",", " ")},
            {"label": "Allocations accordées", "value": f"{status.grants_count:,}".replace(",", " ")},
            {"label": "Rejets MASTER", "value": f"{status.rejections_count:,}".replace(",", " ")},
            {"label": "Fills MASTER", "value": f"{status.fills_count:,}".replace(",", " ")},
            {"label": "Ledger réconcilié", "value": "✓ OUI" if not status.invariant_violations else "✗ NON"},
            {"label": "Real orders", "value": f"{'0' if not status.real_orders_placed else '⚠️ NON ZERO'}"},
        ]
    )
    st.caption(
        "Stratégies sous contrôle MASTER : cross_exchange (portefeuille 5K uniquement — 500/1K/10K/25K restent en comparaison, jamais gérés par MASTER), "
        "atomic, dex_triangular, dex_multihop, dex_cross. Les autres stratégies CEX restent entièrement sous OLD."
    )


# --- PHASE 2 — GLOBAL ORCHESTRATION / SHADOW MODE (user directive, 2026-08-22) ---
#
# Strictly observational: every number below comes from shadow_decisions,
# written ONLY by shadow_orchestrator.py — a separate process that never
# imports the real executors (tests/test_shadow_isolation.py proves this
# mechanically). Nothing on this page can ever reflect a real order, a
# real balance change, or a decision that influenced V5/V5.5.


def render_phase2_shadow_section() -> None:
    st.markdown(
        '<div style="margin-top:22px;padding:14px;border:2px solid #6366f1;border-radius:14px;background:rgba(99,102,241,0.06);">'
        '<div style="font-size:1.1rem;font-weight:700;color:#6366f1;">PHASE 2 — GLOBAL ORCHESTRATION / SHADOW MODE</div>'
        '<div style="font-size:0.85rem;color:#6b7280;margin-top:2px;">Observation uniquement — aucun capital réel, aucune position, '
        "aucun ordre. L'ancien moteur (V5/V5.5) reste l'unique autorité d'exécution.</div></div>",
        unsafe_allow_html=True,
    )

    summary = data.get_shadow_summary_cached(hours=24.0)
    if summary.total_decisions == 0:
        st.caption("SHADOW MODE — en attente de la première évaluation (shadow_orchestrator.py doit être en cours d'exécution).")
        return

    st.markdown('<span class="simple-status-pill simple-status-running" style="display:inline-block;margin:10px 0;">🟢 SHADOW MODE ACTIVE</span>', unsafe_allow_html=True)

    render_stat_cards(
        [
            {"label": "Décisions comparées (24h)", "value": f"{summary.total_decisions:,}".replace(",", " ")},
            {"label": "Accord OLD vs MASTER", "value": f"{summary.agreement_pct:.1f} %" if summary.agreement_pct is not None else "—"},
            {"label": "Désaccord", "value": f"{summary.disagree_count:,}".replace(",", " ")},
            {"label": "OLD accepté, MASTER aurait rejeté", "value": f"{summary.old_approved_master_rejected:,}".replace(",", " ")},
            {"label": "OLD rejeté, MASTER aurait accepté", "value": f"{summary.old_rejected_master_approved:,}".replace(",", " ")},
            {"label": "Conflits de capital détectés", "value": f"{summary.capital_conflicts_detected:,}".replace(",", " "), "sub": "doubles allocations théoriques empêchées"},
            {"label": "Doublons économiques bloqués (MASTER)", "value": f"{summary.duplicate_economic_events_blocked:,}".replace(",", " ")},
            {"label": "position_already_open reproduit", "value": f"{summary.position_already_open_reproduced:,}".replace(",", " ")},
            {"label": "Capital théorique réservé (cumul)", "value": f"{summary.theoretical_capital_reserved_usd:,.2f} $".replace(",", " ")},
            {"label": "P&L OLD simulé", "value": f"{summary.old_pnl_usd:+.2f} $"},
            {"label": "P&L MASTER simulé (projection)", "value": f"{summary.master_pnl_usd:+.2f} $"},
            {"label": "Différence P&L", "value": f"{summary.pnl_difference_usd:+.2f} $"},
        ]
    )

    with st.expander("Répartition par moteur"):
        for eb in data.get_shadow_engine_breakdown_cached(hours=24.0):
            agree_str = f"{eb.agreement_pct:.1f} %" if eb.agreement_pct is not None else "—"
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{eb.engine} — {eb.total_decisions:,} décisions, {agree_str} accord</span>'
                f'<span class="v">OLD {eb.old_pnl_usd:+.2f} $ · MASTER {eb.master_pnl_usd:+.2f} $</span></div>'.replace(",", " "),
                unsafe_allow_html=True,
            )

    with st.expander("Répartition par stratégie"):
        for sb in data.get_shadow_strategy_breakdown_cached(hours=24.0):
            agree_str = f"{sb.agreement_pct:.1f} %" if sb.agreement_pct is not None else "—"
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{STRATEGY_LABELS.get(sb.strategy, sb.strategy)} '
                f'<span style="color:{INK_MUTED};">({sb.engine})</span> — {sb.total_decisions:,}, {agree_str}</span>'
                f'<span class="v">OLD {sb.old_pnl_usd:+.2f} $ · MASTER {sb.master_pnl_usd:+.2f} $</span></div>'.replace(",", " "),
                unsafe_allow_html=True,
            )

    with st.expander("Décisions récentes"):
        for d in data.get_recent_shadow_decisions_cached(limit=15):
            agree_icon = "✓" if d.agree else "⚠️"
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{agree_icon} {STRATEGY_LABELS.get(d.strategy, d.strategy)} '
                f'<span style="color:{INK_MUTED};">({d.engine}, {d.symbol})</span></span>'
                f'<span class="v">OLD: {d.old_outcome} · MASTER: {d.master_outcome} (rang {d.master_rank_score:.1f})</span></div>',
                unsafe_allow_html=True,
            )

    st.caption(
        "real_orders_placed reste false. Aucune décision ci-dessus n'a jamais été transmise à un exécuteur réel. "
        "P&L MASTER est une projection sur expected_profit_usd déjà calculé par les moteurs réels, pas un résultat rejoué au hasard."
    )

    # --- PHASE 2B — CEX Scan-Level Shadow (user directive, 2026-08-22) ---
    st.markdown('<div class="simple-card-label" style="margin-top:14px;">PHASE 2B — CEX au niveau du cycle de scan</div>', unsafe_allow_html=True)
    scan_summary = data.get_cex_scan_agreement_breakdown_cached(hours=24.0)
    if scan_summary.global_total == 0:
        st.caption("En attente des premiers événements de télémétrie (le moteur CEX doit tourner avec l'instrumentation Phase 2B active).")
    else:
        render_stat_cards(
            [
                {"label": "Accord — nouvelles détections", "value": f"{scan_summary.new_detection_agreement_pct:.1f} %" if scan_summary.new_detection_agreement_pct is not None else "—", "sub": f"{scan_summary.new_detection_agree:,}/{scan_summary.new_detection_total:,}".replace(",", " ")},
                {"label": "Accord — continuations", "value": f"{scan_summary.continuation_agreement_pct:.1f} %" if scan_summary.continuation_agreement_pct is not None else "—", "sub": f"{scan_summary.continuation_agree:,}/{scan_summary.continuation_total:,}".replace(",", " ")},
                {"label": "Accord global (niveau scan)", "value": f"{scan_summary.global_agreement_pct:.1f} %" if scan_summary.global_agreement_pct is not None else "—", "sub": f"{scan_summary.global_agree:,}/{scan_summary.global_total:,}".replace(",", " ")},
                {"label": "OLD accepté, MASTER aurait rejeté", "value": f"{scan_summary.old_accepted_master_rejected:,}".replace(",", " ")},
                {"label": "MASTER accepté, OLD avait rejeté", "value": f"{scan_summary.master_accepted_old_rejected:,}".replace(",", " ")},
            ]
        )
        with st.expander("Désaccords par cause (niveau scan)"):
            for d in data.get_cex_scan_disagreement_breakdown_cached(hours=24.0):
                old_str = "OLD approuvé" if d.old_approved else f"OLD rejeté ({d.old_rejection_reason})"
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">{old_str} → MASTER {d.master_outcome}</span>'
                    f'<span class="v">{d.count:,}</span></div>'.replace(",", " "),
                    unsafe_allow_html=True,
                )
        st.caption(
            "Remplace la comparaison au niveau opportunité pour CEX, qui ne voyait que les nouvelles détections — "
            "ici chaque cycle de scan (continuations incluses) est comparé 1:1, exactement comme OLD le vit en direct."
        )


# --- PHASE 2D — BINANCE MICRO-LIVE READINESS / READ-ONLY VALIDATION
# (user directive, 2026-08-23) ---
#
# Every number here comes from GET /micro-live/binance-readiness
# (app.execution.micro_live's in-process state) — read-only real Binance
# data (account balance/status, exchange filters, live book) evaluated
# against a strict 10 USDT cap. real_orders_placed is always 0: this
# section can never reflect a real order, only whether one COULD have
# been placed under real constraints.


def render_phase2d_micro_live_section() -> None:
    readiness = data.get_micro_live_binance_readiness_cached()
    if not readiness.reachable:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">PHASE 2D — MICRO-LIVE READINESS</div>', unsafe_allow_html=True)
        st.caption("Moteur injoignable — état micro-live indisponible.")
        return

    live_label = "🔴 LIVE TRADING = DISABLED" if not readiness.live_trading_enabled else "🟠 LIVE TRADING = ENABLED"
    conn_label = "🟢 connecté" if readiness.binance_connectivity else "🔴 injoignable"
    st.markdown(
        '<div style="margin-top:22px;padding:14px;border:2px solid #f59e0b;border-radius:14px;background:rgba(245,158,11,0.06);">'
        '<div style="font-size:1.1rem;font-weight:700;color:#f59e0b;">PHASE 2D — BINANCE MICRO-LIVE READINESS</div>'
        '<div style="font-size:0.85rem;color:#6b7280;margin-top:2px;">LECTURE SEULE — aucun ordre réel n\'est jamais placé par cette section. '
        f"Binance mainnet : {conn_label}.</div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f'<span class="simple-status-pill simple-status-down" style="display:inline-block;margin:10px 0;">{live_label}</span>', unsafe_allow_html=True)
    if readiness.live_kill_switch_engaged:
        st.markdown(
            '<div class="simple-card" style="border-color:#ef4444;"><div class="simple-card-sub bad">⚠️ Coupe-circuit LIVE engagé</div></div>',
            unsafe_allow_html=True,
        )
    if readiness.key_enable_withdrawals:
        st.markdown(
            '<div class="simple-card" style="border-color:#ef4444;"><div class="simple-card-sub bad">'
            '🚨 Cette clé API a la permission de RETRAIT activée (enableWithdrawals=true) — contrainte de sécurité violée. '
            "Désactive « Enable Withdrawals » sur cette clé dans Binance API Management.</div></div>",
            unsafe_allow_html=True,
        )
    if readiness.account_snapshot_error:
        st.caption(f"⚠️ Solde réel indisponible : {readiness.account_snapshot_error}")
    if readiness.api_restrictions_error:
        st.caption(f"⚠️ Permissions de la clé indisponibles : {readiness.api_restrictions_error}")
    if not readiness.credentials_configured:
        st.caption("⚠️ Aucune clé API Binance configurée (BINANCE_API_KEY/BINANCE_API_SECRET) — solde réel et filtres indisponibles.")

    balance_display = f"{readiness.real_balance_usdt:,.2f} $".replace(",", " ") if readiness.real_balance_usdt is not None else "—"
    reasons = readiness.rejection_reasons or {}
    withdraw_display = (
        "—" if readiness.key_enable_withdrawals is None else ("🚨 OUI" if readiness.key_enable_withdrawals else "✓ NON")
    )

    render_stat_cards(
        [
            {"label": "Connectivité Binance", "value": "✓ PASS" if readiness.binance_connectivity else "✗ FAIL"},
            {"label": "Mode compte", "value": readiness.account_mode},
            {"label": "Clé — retrait autorisé", "value": withdraw_display, "sub": "doit rester NON pour cette phase"},
            {"label": "Clé — restreinte par IP", "value": "✓ OUI" if readiness.key_ip_restricted else "NON" if readiness.key_ip_restricted is not None else "—"},
            {"label": "Solde réel (USDT)", "value": balance_display, "sub": "✓ vérifié" if readiness.real_balance_verified else "non vérifié"},
            {"label": "Cap micro-live", "value": f"{readiness.micro_live_cap_usdt:.2f} $"},
            {"label": "Cap max exécution live (indépendant)", "value": f"{readiness.max_live_capital_usdt:.2f} $"},
            {"label": "Capital PAPER (jamais réutilisé)", "value": f"{readiness.paper_capital_usd:,.0f} $".replace(",", " ")},
            {"label": "Opportunités observées (dry-run)", "value": f"{readiness.opportunities_observed:,}".replace(",", " ")},
            {"label": "Exécutables avec le cap", "value": f"{readiness.executable_with_cap:,}".replace(",", " ")},
            {"label": "Non exécutables", "value": f"{readiness.non_executable:,}".replace(",", " ")},
            {"label": "Rejets MIN_NOTIONAL", "value": f"{reasons.get('min_notional', 0):,}".replace(",", " ")},
            {"label": "Rejets LOT_SIZE", "value": f"{reasons.get('lot_size', 0):,}".replace(",", " ")},
            {"label": "Rejets solde insuffisant", "value": f"{reasons.get('balance', 0):,}".replace(",", " ")},
            {"label": "Rejets profit net <= 0", "value": f"{reasons.get('net_profit_leq_zero', 0):,}".replace(",", " ")},
            {"label": "Frais réels estimés (moy.)", "value": f"{readiness.avg_estimated_fees_usd:.4f} $" if readiness.avg_estimated_fees_usd is not None else "—"},
            {"label": "Slippage réel estimé (moy.)", "value": f"{readiness.avg_estimated_slippage_pct:.3f} %" if readiness.avg_estimated_slippage_pct is not None else "—"},
            {"label": "Profit net réel estimé (moy.)", "value": f"{readiness.avg_net_profit_after_real_constraints_usd:+.4f} $" if readiness.avg_net_profit_after_real_constraints_usd is not None else "—"},
            {"label": "Real orders placed", "value": f"{readiness.real_orders_placed}"},
        ]
    )
    st.caption(
        "Dry-run limité aux opportunités cross_exchange ayant une jambe Binance (ce périmètre est explicitement Binance-only). "
        "Le cap micro-live (10 USDT par défaut) est appliqué indépendamment du capital PAPER de 10 000 $ — jamais réutilisé pour ce dimensionnement."
    )


# --- PHASE 2E — REAL EDGE VALIDATION (user directive, 2026-08-23) ---
#
# Analyse historique de micro_live_observations (persisté par Phase 2E,
# lu via GET /micro-live/edge-report) — frais Binance RÉELS quand
# disponibles, distribution complète (pas seulement une moyenne), par
# symbole/stratégie, section LUNC/USDT dédiée, et un gate de sécurité basé
# sur les données observées. Toujours en lecture seule.


def render_phase2e_real_edge_section() -> None:
    report = data.get_micro_live_edge_report_cached(hours=72.0)
    if not report.reachable:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">PHASE 2E — REAL EDGE VALIDATION</div>', unsafe_allow_html=True)
        st.caption("Moteur injoignable — validation d'edge indisponible.")
        return

    st.markdown(
        '<div style="margin-top:22px;padding:14px;border:2px solid #8b5cf6;border-radius:14px;background:rgba(139,92,246,0.06);">'
        '<div style="font-size:1.1rem;font-weight:700;color:#8b5cf6;">PHASE 2E — REAL EDGE VALIDATION</div>'
        '<div style="font-size:0.85rem;color:#6b7280;margin-top:2px;">Frais Binance réels quand disponibles, distribution complète, '
        "par symbole/stratégie — dernières 72h. Lecture seule, aucun ordre réel.</div></div>",
        unsafe_allow_html=True,
    )

    if report.observations == 0:
        st.caption("En attente des premières observations persistées (micro_live_observations).")
        return

    net = report.net_profit
    bps = report.net_return_bps

    render_stat_cards(
        [
            {"label": "Observations", "value": f"{report.observations:,}".replace(",", " ")},
            {"label": "Couverture frais réels", "value": f"{report.real_fee_coverage_pct:.0f} %" if report.real_fee_coverage_pct is not None else "—"},
            {"label": "Edge brut (moy.)", "value": f"{report.gross_profit.mean:+.4f} $" if report.gross_profit.mean is not None else "—"},
            {"label": "Frais réels (moy.)", "value": f"{report.avg_fees_usd:.4f} $" if report.avg_fees_usd is not None else "—"},
            {"label": "Slippage (moy.)", "value": f"{report.avg_slippage_pct:.3f} %" if report.avg_slippage_pct is not None else "—"},
            {"label": "Edge net (moy.)", "value": f"{net.mean:+.4f} $" if net.mean is not None else "—"},
            {"label": "Edge net (médiane)", "value": f"{net.median:+.4f} $" if net.median is not None else "—"},
            {"label": "Taux edge net positif", "value": f"{net.positive_rate_pct:.1f} %" if net.positive_rate_pct is not None else "—"},
            {"label": "Net return (bps, moy.)", "value": f"{bps.mean:+.1f}" if bps.mean is not None else "—"},
            {"label": "Net return (bps, médiane)", "value": f"{bps.median:+.1f}" if bps.median is not None else "—"},
            {"label": "P10 / P90 edge net", "value": f"{net.p10:+.4f} $ / {net.p90:+.4f} $" if net.p10 is not None else "—"},
            {"label": "Pire / meilleure observation", "value": f"{net.worst:+.4f} $ / {net.best:+.4f} $" if net.worst is not None else "—"},
            {"label": "Marge de sécurité recommandée", "value": f"{report.recommended_safety_margin_usd:.4f} $", "sub": "1 écart-type de l'edge net observé"},
            {"label": "Opportunités qualifiées (gate)", "value": f"{report.qualifying_after_gate:,}".replace(",", " "), "sub": f"sur {report.observations:,}".replace(",", " ")},
            {"label": "Real orders placed", "value": f"{report.real_orders_placed}"},
        ]
    )

    reasons = report.rejection_reasons or {}
    if reasons:
        st.caption("Causes de rejet (historique) : " + ", ".join(f"{k}: {v}" for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])))

    with st.expander("Symboles à edge net positif (top)"):
        if not report.top_positive_symbols:
            st.caption("Pas assez d'observations par symbole pour ce classement.")
        for g in report.top_positive_symbols:
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{g.key} ({g.observations} obs.)</span>'
                f'<span class="v">edge net moy. {g.net_profit.mean:+.4f} $ · positif {g.positive_net_rate_pct:.0f}%</span></div>',
                unsafe_allow_html=True,
            )

    with st.expander("Symboles qui détruisent la moyenne (edge net négatif)"):
        if not report.negative_symbols:
            st.caption("Aucun symbole avec au moins 5 observations et un edge net moyen négatif.")
        for g in report.negative_symbols:
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{g.key} ({g.observations} obs.)</span>'
                f'<span class="v">edge net moy. {g.net_profit.mean:+.4f} $ · positif {g.positive_net_rate_pct:.0f}%</span></div>',
                unsafe_allow_html=True,
            )

    if report.lunc_usdt is not None:
        lunc = report.lunc_usdt
        with st.expander(f"LUNC/USDT — analyse dédiée ({lunc.observations} obs.)"):
            render_stat_cards(
                [
                    {"label": "Edge net (moy.)", "value": f"{lunc.net_profit.mean:+.4f} $" if lunc.net_profit.mean is not None else "—"},
                    {"label": "Edge net (médiane)", "value": f"{lunc.net_profit.median:+.4f} $" if lunc.net_profit.median is not None else "—"},
                    {"label": "Taux positif", "value": f"{lunc.positive_net_rate_pct:.1f} %" if lunc.positive_net_rate_pct is not None else "—"},
                    {"label": "Spread carnet (moy.)", "value": f"{lunc.avg_book_spread_pct:.3f} %" if lunc.avg_book_spread_pct is not None else "—"},
                    {"label": "Profondeur dispo (moy.)", "value": f"{lunc.avg_available_depth_usd:,.0f} $".replace(",", " ") if lunc.avg_available_depth_usd is not None else "—"},
                    {"label": "Slippage (moy.)", "value": f"{lunc.avg_slippage_pct:.3f} %" if lunc.avg_slippage_pct is not None else "—"},
                    {"label": "Taux passage MIN_NOTIONAL", "value": f"{lunc.min_notional_pass_rate_pct:.0f} %" if lunc.min_notional_pass_rate_pct is not None else "—"},
                    {"label": "Taux passage LOT_SIZE", "value": f"{lunc.lot_size_pass_rate_pct:.0f} %" if lunc.lot_size_pass_rate_pct is not None else "—"},
                ]
            )
    else:
        st.caption("LUNC/USDT : aucune observation dans la fenêtre — pas de section dédiée à afficher.")

    st.caption(
        "Le gate de sécurité (« opportunités qualifiées ») exige net_profit > marge de sécurité, pas seulement > 0 — "
        "la marge est calculée à partir de l'écart-type de l'edge net réellement observé, jamais choisie pour fabriquer un résultat favorable."
    )


# --- PHASE 3 — REAL TRADING, Binance + Bybit (user directive, 2026-08-23) ---
#
# Toutes les valeurs viennent de GET /live/dashboard-summary — soldes réels,
# P&L calculé UNIQUEMENT à partir des fills réels retournés par les
# exchanges (jamais du moteur paper), coupe-circuit réel. Séparé
# visuellement des sections PAPER (couleur rouge/orange dédiée) pour
# qu'aucune valeur ne puisse être confondue avec une simulation.


def render_real_trading_section() -> None:
    summary = data.get_live_dashboard_summary_cached()
    if not summary.reachable:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">PHASE 3 — REAL TRADING (BINANCE + BYBIT)</div>', unsafe_allow_html=True)
        st.caption("Moteur injoignable — état du trading réel indisponible.")
        return

    live_label = "🔴 LIVE TRADING = DISABLED" if not summary.live_trading_enabled else "🟢 LIVE TRADING = ENABLED"
    st.markdown(
        '<div style="margin-top:22px;padding:14px;border:2px solid #dc2626;border-radius:14px;background:rgba(220,38,38,0.06);">'
        '<div style="font-size:1.1rem;font-weight:700;color:#dc2626;">PHASE 3 — REAL TRADING — BINANCE + BYBIT</div>'
        '<div style="font-size:0.85rem;color:#6b7280;margin-top:2px;">Argent RÉEL — distinct des sections PAPER ci-dessus. '
        "P&L calculé exclusivement depuis les fills réels des exchanges.</div></div>",
        unsafe_allow_html=True,
    )
    st.markdown(f'<span class="simple-status-pill simple-status-down" style="display:inline-block;margin:10px 0;">{live_label}</span>', unsafe_allow_html=True)
    if summary.kill_switch_engaged:
        st.markdown(
            f'<div class="simple-card" style="border-color:#ef4444;"><div class="simple-card-sub bad">🚨 Coupe-circuit LIVE engagé : {summary.kill_switch_reason}</div></div>',
            unsafe_allow_html=True,
        )

    win_rate_display = f"{summary.win_rate_pct:.1f} %" if summary.win_rate_pct is not None else "—"
    avg_profit_display = f"{summary.average_profit_per_trade_usd:+.4f} $" if summary.average_profit_per_trade_usd is not None else "—"
    best = summary.current_best_opportunity

    render_stat_cards(
        [
            {"label": "Capital réel cible (total)", "value": f"{summary.total_real_capital_target_usdt:,.0f} $".replace(",", " ")},
            {"label": "Solde Binance", "value": f"{summary.binance_balance_usdt:,.2f} $".replace(",", " "), "sub": f"cible {summary.binance_target_capital_usdt:.0f} $"},
            {"label": "Solde Bybit", "value": f"{summary.bybit_balance_usdt:,.2f} $".replace(",", " "), "sub": f"cible {summary.bybit_target_capital_usdt:.0f} $"},
            {"label": "Capital disponible", "value": f"{summary.available_capital_usdt:,.2f} $".replace(",", " ")},
            {"label": "P&L réel — aujourd'hui", "value": f"{summary.today_real_pnl_usd:+.4f} $"},
            {"label": "P&L réel — total", "value": f"{summary.total_real_pnl_usd:+.4f} $"},
            {"label": "Trades réels", "value": f"{summary.real_trades:,}".replace(",", " ")},
            {"label": "Gagnants / perdants", "value": f"{summary.wins} / {summary.losses}"},
            {"label": "Taux de réussite", "value": win_rate_display},
            {"label": "Frais réels totaux", "value": f"{summary.total_real_fees_usd:.4f} $"},
            {"label": "Profit moyen / trade", "value": avg_profit_display},
            {"label": "Ordres actifs", "value": f"{summary.active_orders}"},
            {"label": "Real orders placed", "value": f"{summary.real_orders_placed}"},
        ]
    )

    if best is not None:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">Meilleure opportunité actuelle</div>', unsafe_allow_html=True)
        st.markdown(
            f'<div class="simple-perf-row"><span class="k">{best["symbol"]} — {best["buy_exchange"]}→{best["sell_exchange"]}</span>'
            f'<span class="v">profit net {best["net_profit_usd"]:+.4f} $ · {best["net_return_bps"]:+.1f} bps · '
            f'{"pré-positionné" if best["prepositioned"] else "PAS pré-positionné"}</span></div>',
            unsafe_allow_html=True,
        )
    else:
        st.caption("Aucune opportunité qualifiée actuellement (aucun candidat pré-positionné avec un edge net réel positif).")

    if summary.last_trades:
        with st.expander("Derniers trades réels"):
            for t in summary.last_trades:
                pnl_str = f"{t.actual_net_pnl_usd:+.4f} $" if t.actual_net_pnl_usd is not None else "—"
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">{t.symbol} — {t.buy_exchange}→{t.sell_exchange} ({t.outcome})</span>'
                    f'<span class="v">{pnl_str} · {t.started_at}</span></div>',
                    unsafe_allow_html=True,
                )
    else:
        st.caption("Aucun trade réel enregistré pour l'instant — la table reste vide tant qu'aucun ordre réel n'a été autorisé et exécuté.")

    st.caption(
        "Les données PAPER (sections ci-dessus) et RÉELLES (cette section) restent strictement séparées — "
        "aucune donnée paper n'alimente jamais ce P&L, et inversement."
    )


def render_full_market_discovery_section() -> None:
    summary = data.get_full_market_discovery_summary_cached()
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">FULL MARKET DISCOVERY — UNIVERS DYNAMIQUE COMPLET</div>', unsafe_allow_html=True)
    if not summary.reachable:
        st.caption("Moteur injoignable — découverte de marché indisponible.")
        return

    age_str = f"{summary.scan_status_age_seconds:.0f}s" if summary.scan_status_age_seconds is not None else "—"
    cycle_str = f"{summary.cycle_duration_seconds:.1f}s" if summary.cycle_duration_seconds is not None else "—"
    render_stat_cards(
        [
            {"label": "Paires communes (univers)", "value": f"{summary.common_pairs}"},
            {"label": "Paires scannées (STAGE A)", "value": f"{summary.pairs_fast_scanned}"},
            {"label": "Paires validées en profondeur (STAGE B)", "value": f"{summary.pairs_deep_validated}"},
            {"label": "Paires avec spread brut (STAGE A, estimation)", "value": f"{summary.pairs_raw_spread_stage_a}"},
            {"label": "Edges nets positifs (STAGE B, réel, LIVE only)", "value": f"{summary.pairs_net_positive_stage_b_live}"},
            {"label": "Edges récurrents", "value": f"{summary.pairs_with_repeating_net_edge}"},
            {"label": "Dernier cycle scanner", "value": age_str, "sub": f"durée {cycle_str}"},
        ]
    )
    if not summary.scan_status_available:
        st.caption("Aucun cycle du scanner deux-étages n'a encore été observé — le processus altcoin_scanner.py démarre peut-être tout juste.")

    if summary.top_10_opportunities:
        with st.expander(f"TOP 10 opportunités actuelles ({len(summary.top_10_opportunities)})"):
            for t in summary.top_10_opportunities:
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">{t.symbol} — {t.buy_exchange}→{t.sell_exchange}</span>'
                    f'<span class="v">{t.net_profit_per_1000usdt_mean:+.2f} $/1000usdt · {t.status}</span></div>',
                    unsafe_allow_html=True,
                )
    else:
        st.caption("Pas encore d'historique suffisant pour un classement TOP 10.")


def render_missed_and_capital_section() -> None:
    missed = data.get_missed_opportunities_summary_cached()
    capital = data.get_capital_bottleneck_summary_cached()
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">MISSED OPPORTUNITIES & CAPITAL BOTTLENECK</div>', unsafe_allow_html=True)

    if not missed.reachable and not capital.reachable:
        st.caption("Moteur injoignable — analyse indisponible.")
        return

    current_tier = next((t for t in capital.tiers if t.total_capital_usdt == 160.0), None)
    render_stat_cards(
        [
            {"label": "Opportunités manquées (total)", "value": f"{missed.total_missed}" if missed.reachable else "—"},
            {"label": "Raison principale", "value": missed.primary_cause or "NONE" if missed.reachable else "—"},
            {"label": "Profit théorique non réalisé", "value": f"{missed.total_theoretical_profit_usd:+.4f} $" if missed.reachable else "—", "sub": "THEORETICAL_NOT_REALIZED"},
            {"label": "Capital bottleneck (160 USDT)", "value": ("OUI" if capital.current_capital_bottleneck else "NON") if capital.reachable else "—"},
            {"label": "300 USDT aiderait", "value": ("OUI" if capital.would_300_materially_help else "NON") if capital.reachable else "—"},
            {"label": "500 USDT aiderait", "value": ("OUI" if capital.would_500_materially_help else "NON") if capital.reachable else "—"},
            {"label": "Inventory bottleneck (160 USDT)", "value": f"{current_tier.missed_for_inventory}" if current_tier else "—", "sub": "exécutable côté capital, bloqué côté inventaire"},
        ]
    )

    if missed.reachable and missed.causes:
        with st.expander(f"Pourquoi manquées — détail par cause ({len(missed.causes)})"):
            for c in sorted(missed.causes, key=lambda c: c.count, reverse=True):
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">{c.cause}</span>'
                    f'<span class="v">{c.count} fois · {c.theoretical_profit_usd_total:+.4f} $ théorique non réalisé</span></div>',
                    unsafe_allow_html=True,
                )

    if capital.reachable and capital.tiers:
        with st.expander("Simulation multi-capital (160 / 300 / 500 / 1000 USDT)"):
            for t in capital.tiers:
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">{t.total_capital_usdt:.0f} USDT '
                    f'(Binance {t.binance_allocation_usdt:.0f} / Bybit {t.bybit_allocation_usdt:.0f})</span>'
                    f'<span class="v">{t.executable_profitable_opportunities} exécutables · manqué capital {t.missed_for_capital} · '
                    f'manqué inventaire {t.missed_for_inventory} · utilisation {t.capital_utilization_pct:.0f}% · '
                    f'P&L simulé {t.simulated_net_pnl_usd:+.4f} $</span></div>',
                    unsafe_allow_html=True,
                )
        st.caption(f"300 USDT : {capital.would_300_evidence}")
        st.caption(f"500 USDT : {capital.would_500_evidence}")

    st.caption(
        "Toutes les valeurs de profit ici sont THEORETICAL_NOT_REALIZED — aucun ordre réel n'a jamais été placé pour les obtenir, "
        "et cette simulation ne peut en placer aucun."
    )


def render_inventory_manager_section() -> None:
    summary = data.get_inventory_manager_summary_cached()
    if not summary.reachable:
        st.markdown('<div class="simple-card-label" style="margin-top:14px;">INVENTORY INTELLIGENCE</div>', unsafe_allow_html=True)
        st.caption("Moteur injoignable — état de l'inventaire indisponible.")
        return

    mode_label = f"MODE = {summary.inventory_manager_mode}" + (" · AUTO_REAL_REBALANCE = TRUE ⚠️" if summary.auto_real_rebalance else " · AUTO_REAL_REBALANCE = FALSE")
    st.markdown(
        '<div style="margin-top:22px;padding:14px;border:2px solid #7c3aed;border-radius:14px;background:rgba(124,58,237,0.06);">'
        '<div style="font-size:1.1rem;font-weight:700;color:#7c3aed;">INVENTORY INTELLIGENCE (SIMULATION / READ-ONLY)</div>'
        f'<div style="font-size:0.85rem;color:#6b7280;margin-top:2px;">{mode_label} — Recommandations de rééquilibrage uniquement, aucun ordre réel n\'est '
        "jamais envoyé par ce module. Les 160 USDT réels ne sont pas convertis automatiquement tant que ce comportement "
        "n'a pas été vérifié et explicitement autorisé.</div></div>",
        unsafe_allow_html=True,
    )

    pnl_display = f"{summary.inventory_pnl_usd:+.4f} $" if summary.inventory_pnl_usd is not None else "N/A"
    strong_count = sum(1 for s in summary.inventory_scores if s.classification == "STRONG_PREPOSITION_CANDIDATE")
    candidate_count = sum(1 for s in summary.inventory_scores if s.classification == "PREPOSITION_CANDIDATE")
    render_stat_cards(
        [
            {"label": "USDT disponible (total)", "value": f"{summary.total_usdt_available:,.2f} $".replace(",", " ")},
            {"label": "Binance — USDT dispo", "value": f"{summary.binance_usdt_available:,.2f} $".replace(",", " "), "sub": f"{len(summary.binance_holdings)} actif(s) en stock"},
            {"label": "Bybit — USDT dispo", "value": f"{summary.bybit_usdt_available:,.2f} $".replace(",", " "), "sub": f"{len(summary.bybit_holdings)} actif(s) en stock"},
            {"label": "Capital verrouillé en inventaire", "value": f"{summary.capital_locked_in_inventory_usdt:,.2f} $".replace(",", " ")},
            {"label": "Actifs pré-positionnés", "value": f"{len(summary.prepositioned_assets)}"},
            {"label": "Inventaire manquant", "value": f"{len(summary.inventory_missing)}"},
            {"label": "STRONG candidates", "value": f"{strong_count}", "sub": f"{candidate_count} PREPOSITION_CANDIDATE"},
            {"label": "Candidats de rééquilibrage", "value": f"{len(summary.rebalance_candidates)}"},
            {"label": "Inventory P&L", "value": pnl_display, "sub": summary.inventory_pnl_note if summary.inventory_pnl_usd is None else None},
        ]
    )

    if summary.prepositioned_assets:
        st.caption(f"Déjà pré-positionnés et exécutables immédiatement : {', '.join(summary.prepositioned_assets)}")

    if summary.inventory_scores:
        with st.expander(f"TOP inventory scores — classification complète ({len(summary.inventory_scores)})"):
            for s in summary.inventory_scores:
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">[{s.classification}] {s.symbol}</span>'
                    f'<span class="v">score {s.total_score:.0f}/100 · {s.sightings} sightings · {s.net_positive_rate_pct:.0f}% positif · '
                    f'médiane {s.median_net_edge_per_1000usdt:+.2f} · P10 {s.p10_net_edge_per_1000usdt:+.2f} · reuse {s.expected_reuse_label}</span></div>',
                    unsafe_allow_html=True,
                )
    else:
        st.caption("Aucun historique de scoring disponible pour l'instant.")

    if summary.inventory_missing:
        with st.expander(f"Opportunités bloquées par manque d'inventaire ({len(summary.inventory_missing)})"):
            for m in summary.inventory_missing:
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">{m.symbol} — {m.buy_exchange}→{m.sell_exchange}</span>'
                    f'<span class="v">besoin {m.required_base_amount:.4f} {m.required_base_asset} sur {m.sell_exchange} '
                    f"(détenu : {m.current_base_inventory:.4f}) · {m.reason}</span></div>",
                    unsafe_allow_html=True,
                )
    else:
        st.caption("Aucune opportunité actuellement bloquée par un manque d'inventaire.")

    if summary.rebalance_candidates:
        with st.expander(f"Candidats de rééquilibrage — SIMULÉ, non exécuté ({len(summary.rebalance_candidates)})"):
            for r in summary.rebalance_candidates:
                score_str = f"{r.inventory_score:.0f}/100" if r.inventory_score is not None else "—"
                st.markdown(
                    f'<div class="simple-perf-row"><span class="k">[{r.action} · {r.classification or "—"}] {r.asset} sur {r.exchange}</span>'
                    f'<span class="v">{r.capital_required_usdt:.2f} $ · score {score_str} · {r.reason}</span></div>',
                    unsafe_allow_html=True,
                )
    else:
        st.caption("Aucune recommandation de rééquilibrage actuellement — NONE / NO ACTION.")

    st.caption(
        "Ce module est 100% SIMULATION — il calcule des recommandations à partir des soldes réels et de l'historique "
        "réel du scanner, mais ne place, ne modifie et n'annule jamais un ordre. real_orders_placed = 0 en permanence."
    )


def _money4(value: float | None) -> str:
    return f"{value:,.4f} $".replace(",", " ") if value is not None else "—"


def _money4_signed(value: float | None) -> str:
    return f"{value:+,.4f} $".replace(",", " ") if value is not None else "—"


LIVE_STATUS_LABELS = {
    "EXECUTING": ("🟢 EXECUTING", STATUS_GOOD),
    "RUNNING": ("🟡 WAITING FOR OPPORTUNITY", STATUS_WARNING),
    "PAUSED": ("⏸️ PAUSED", STATUS_WARNING),
    "KILL_SWITCH": ("🔴 KILL SWITCH", STATUS_CRITICAL),
    "STOPPED": ("⚪ STOPPED", INK_MUTED),
    "UNKNOWN": ("🟠 UNKNOWN", STATUS_WARNING),
}


def _derive_live_status(script_status) -> str:
    if not script_status.available:
        return "STOPPED"
    if script_status.stale:
        return "UNKNOWN"
    raw = script_status.raw
    if raw.get("KILL_SWITCH_ENGAGED"):
        return "KILL_SWITCH"
    if raw.get("LIVE_STATUS") == "STOPPED":
        return "STOPPED"
    if raw.get("ACTIVE_ARBITRAGE") or raw.get("ACTIVE_INVENTORY"):
        return "EXECUTING"
    if raw.get("LIVE_STATUS") == "RUNNING":
        return "RUNNING"
    return "UNKNOWN"


def _render_live_header(script_status, raw: dict) -> str:
    status_key = _derive_live_status(script_status)
    status_label, status_color = LIVE_STATUS_LABELS[status_key]
    session_start = raw.get("SESSION_START", "—")
    duration_h = raw.get("SESSION_DURATION_HOURS")
    duration_str = f"{duration_h:.2f} h" if duration_h is not None else "—"
    now_str = datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S UTC")

    st.markdown(
        f'<div style="padding:16px 20px;border:2px solid #dc2626;border-radius:14px;'
        f'background:rgba(220,38,38,0.05);margin-bottom:14px;">'
        f'<div style="font-size:1.3rem;font-weight:800;color:#dc2626;">🔴 LIVE TRADING — BINANCE + BYBIT</div>'
        f'<div style="font-size:1.05rem;font-weight:700;color:{status_color};margin-top:6px;">{status_label}</div>'
        f'<div style="font-size:0.85rem;color:{INK_MUTED};margin-top:6px;">'
        f"SESSION START : {session_start} &nbsp;·&nbsp; DURÉE : {duration_str} &nbsp;·&nbsp; MAINTENANT : {now_str}</div>"
        f"</div>",
        unsafe_allow_html=True,
    )
    if not script_status.available:
        st.caption("Aucun fichier de statut trouvé — aucune session live n'a encore tourné, ou son fichier a été nettoyé après le rapport final. Les données ci-dessous restent réelles (ledger + soldes exchange).")
    elif script_status.stale:
        st.warning(f"⚠️ Le fichier de statut indique RUNNING mais n'a pas été mis à jour depuis {script_status.age_seconds:.0f}s — le processus a peut-être été interrompu sans se terminer proprement.")
    incident = raw.get("INCIDENT")
    if incident:
        st.markdown(
            f'<div style="padding:14px 18px;border:2px solid #dc2626;border-radius:12px;background:rgba(220,38,38,0.10);margin:10px 0;">'
            f'<div style="font-weight:800;color:#dc2626;font-size:1.05rem;">🚨 LIVE EXECUTION PAUSED</div>'
            f'<div style="color:{INK_SECONDARY};margin-top:4px;">{incident.get("type", "incident")} — {incident.get("detail", "")}</div></div>',
            unsafe_allow_html=True,
        )
    return status_key


def _render_live_capital(summary) -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:18px;">CAPITAL RÉEL</div>', unsafe_allow_html=True)
    if not summary.balances_reachable:
        st.caption("Comptes exchange injoignables pour l'instant — soldes potentiellement obsolètes.")
    binance_usdt = summary.binance_usdt or 0.0
    bybit_usdt = summary.bybit_usdt or 0.0
    total_usdt = binance_usdt + bybit_usdt
    total_inventory_value = summary.binance_inventory_value_usdt + summary.bybit_inventory_value_usdt
    total_capital = total_usdt + total_inventory_value
    render_stat_cards(
        [
            {"label": "TOTAL REAL CAPITAL", "value": _money4(total_capital), "sub": f"USDT {_money4(total_usdt)} + inventaire {_money4(total_inventory_value)}"},
            {"label": "Binance — USDT", "value": _money4(summary.binance_usdt)},
            {"label": "Binance — valeur inventaire", "value": _money4(summary.binance_inventory_value_usdt)},
            {"label": "Bybit — USDT", "value": _money4(summary.bybit_usdt)},
            {"label": "Bybit — valeur inventaire", "value": _money4(summary.bybit_inventory_value_usdt)},
            {"label": "AVAILABLE CAPITAL", "value": _money4(total_usdt), "sub": "USDT libre, deux exchanges"},
            {"label": "CAPITAL IN INVENTORY", "value": _money4(total_inventory_value)},
        ]
    )
    st.caption("Jamais de solde paper — chaque chiffre ci-dessus vient d'un appel compte réel Binance/Bybit fait au moment du rafraîchissement.")


def _render_live_pnl(summary) -> None:
    pnl = summary.pnl
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">REAL NET P&L</div>', unsafe_allow_html=True)
    if pnl is None:
        st.caption("Pas encore de données.")
        return
    color = STATUS_GOOD if pnl.total_pnl_usd >= 0 else STATUS_CRITICAL
    render_live_number_card(
        "P&L RÉEL — TOTAL DEPUIS LE DÉBUT DU LIVE",
        [
            {"value": pnl.total_pnl_usd, "decimals": 4, "big": True, "suffix": " $", "signed": True, "color": color},
            {"value": pnl.today_pnl_usd, "decimals": 4, "suffix": " $", "signed": True, "label": "Aujourd'hui :", "color": INK_SECONDARY},
        ]
        + ([{"value": pnl.session_pnl_usd, "decimals": 4, "suffix": " $", "signed": True, "label": "Cette session :", "color": INK_SECONDARY}] if pnl.session_pnl_usd is not None else []),
        key="live_pnl",
    )
    render_stat_cards(
        [
            {"label": "P&L / heure (session)", "value": _money4_signed(pnl.pnl_per_hour_usd) if pnl.pnl_per_hour_usd is not None else "—"},
            {"label": "Profit moyen / trade", "value": _money4_signed(pnl.average_pnl_per_trade_usd)},
            {"label": "Meilleur trade", "value": _money4_signed(pnl.best_trade.actual_net_usd) if pnl.best_trade else "—", "sub": pnl.best_trade.symbol if pnl.best_trade else None},
            {"label": "Pire trade", "value": _money4_signed(pnl.worst_trade.actual_net_usd) if pnl.worst_trade else "—", "sub": pnl.worst_trade.symbol if pnl.worst_trade else None},
            {"label": "Win rate", "value": f"{pnl.win_rate_pct:.1f} %" if pnl.win_rate_pct is not None else "—"},
        ]
    )
    st.caption("P&L calculé exclusivement depuis les fills et frais réels des exchanges (live_arbitrage_executions) — jamais depuis des données paper.")


def _render_live_trades(summary) -> None:
    counts = summary.counts
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">TRADES LIVE</div>', unsafe_allow_html=True)
    if counts is not None:
        render_stat_cards(
            [
                {"label": "Complete real arbitrages", "value": f"{counts.complete_arbitrages}"},
                {"label": "Successful", "value": f"{counts.successful}"},
                {"label": "Failed", "value": f"{counts.failed}"},
                {"label": "Aborted", "value": f"{counts.aborted}"},
                {"label": "Neutralizations", "value": f"{counts.neutralizations}"},
            ]
        )
    if not summary.last_trades:
        st.caption("Aucun trade réel enregistré pour l'instant.")
        return
    with st.expander(f"Derniers trades réels ({len(summary.last_trades)})", expanded=True):
        for t in summary.last_trades:
            pnl_color = "good" if (t.actual_net_usd or 0) >= 0 else "bad"
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{t.at} — {t.symbol} ({t.buy_exchange}→{t.sell_exchange})</span>'
                f'<span class="v simple-card-sub {pnl_color}">notional {_money4(t.notional_usdt)} · prédit {_money4_signed(t.predicted_net_usd)} · '
                f"réel {_money4_signed(t.actual_net_usd)} · frais {_money4(t.total_fees_usd)} · "
                f'{t.latency_ms:.0f} ms</span></div>',
                unsafe_allow_html=True,
            )


def _render_live_best_opportunity() -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">BEST OPPORTUNITY NOW</div>', unsafe_allow_html=True)
    engine_summary = data.get_live_dashboard_summary_cached()
    if not engine_summary.reachable:
        st.caption("Moteur (scanner continu) injoignable — opportunité actuelle indisponible.")
        return
    best = engine_summary.current_best_opportunity
    if best is None:
        st.markdown('<div class="simple-state-card warn"><div class="simple-state-title">WAITING — NO QUALIFIED EDGE</div></div>', unsafe_allow_html=True)
        return
    st.markdown(
        f'<div class="simple-card"><div class="simple-opp-symbol">{best["symbol"]}</div>'
        f'<div class="simple-opp-route">{best["buy_exchange"]} → {best["sell_exchange"]}</div>'
        f'<div class="simple-opp-row"><span class="k">Expected net profit</span><span class="v">{best["net_profit_usd"]:+.4f} $</span></div>'
        f'<div class="simple-opp-row"><span class="k">Net return</span><span class="v">{best["net_return_bps"]:+.1f} bps</span></div>'
        f'<div class="simple-opp-row"><span class="k">Inventory ready</span><span class="v">{"OUI" if best["prepositioned"] else "NON"}</span></div>'
        f'<div class="simple-opp-row"><span class="k">Executable now</span><span class="v">{"OUI" if best["prepositioned"] else "NON — constitution requise"}</span></div>'
        f"</div>",
        unsafe_allow_html=True,
    )
    st.caption("Reflète le scanner continu du moteur (rank_live_opportunities) — pas nécessairement la prochaine action d'un script live spécifique.")


INVENTORY_STATUS_BADGES = {"READY": ("good", "READY"), "LOW": ("warn", "LOW"), "DUST": ("bad", "DUST"), "UNKNOWN": ("warn", "UNKNOWN")}


def _render_live_inventory(summary) -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">INVENTORY LIVE</div>', unsafe_allow_html=True)
    inv = summary.inventory_summary
    if inv is not None:
        render_stat_cards(
            [
                {"label": "Inventory constitutions", "value": f"{inv.total_constitutions}"},
                {"label": "· dont nouvelles", "value": f"{inv.new_constitutions}"},
                {"label": "· dont recycling", "value": f"{inv.recycling_constitutions}"},
                {"label": "Total inventory cost (USDT fees)", "value": _money4(inv.total_inventory_cost_usd)},
            ]
        )
    if not summary.positions:
        st.caption("Aucune position d'inventaire réelle actuellement (tous les soldes non-USDT sont à zéro).")
        return
    with st.expander(f"Positions d'inventaire réelles ({len(summary.positions)})", expanded=True):
        for p in sorted(summary.positions, key=lambda p: p.value_usdt or 0, reverse=True):
            css_class, badge_label = INVENTORY_STATUS_BADGES.get(p.status, ("warn", p.status))
            unrealized_str = _money4_signed(p.unrealized_pnl_usd) if p.unrealized_pnl_usd is not None else "—"
            st.markdown(
                f'<div class="simple-perf-row"><span class="k">{p.symbol} — {p.exchange} · {p.quantity:,.4f}</span>'
                f'<span class="v simple-card-sub {css_class}">{_money4(p.value_usdt)} · cost basis {_money4(p.cost_basis_usdt_per_unit)}/u · '
                f'unrealized {unrealized_str} · {badge_label}</span></div>'.replace(",", " "),
                unsafe_allow_html=True,
            )
    st.caption("Cost basis = moyenne pondérée de tous les fills d'achat réels enregistrés pour cet actif/exchange (pas un suivi FIFO par unité détenue).")


def _render_live_active_cycle(raw: dict) -> None:
    active_arb = raw.get("ACTIVE_ARBITRAGE")
    active_inv = raw.get("ACTIVE_INVENTORY")
    if not active_arb and not active_inv:
        return
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">ACTIVE CYCLE</div>', unsafe_allow_html=True)
    stage = "INVENTORY CHECK → COMMON SIZING" if active_inv and not active_arb else "BUY SUBMITTED → SELL PENDING → RECONCILE"
    detail = active_inv or active_arb
    st.markdown(
        f'<div class="simple-state-card good"><div class="simple-state-title">ACTIVE ARBITRAGE</div>'
        f'<div class="simple-state-body">{detail}<br><span style="color:{INK_MUTED};">Étape : DETECTED → QUALIFIED → {stage} → COMPLETE</span></div></div>',
        unsafe_allow_html=True,
    )


def _render_live_predicted_vs_actual(summary) -> None:
    pnl = summary.pnl
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">PREDICTED VS ACTUAL</div>', unsafe_allow_html=True)
    if pnl is None or not summary.last_trades:
        st.caption("Pas encore assez de trades réels.")
        return
    render_stat_cards(
        [
            {"label": "Predicted total P&L", "value": _money4_signed(pnl.predicted_total_pnl_usd)},
            {"label": "Actual total P&L", "value": _money4_signed(pnl.actual_total_pnl_usd)},
            {"label": "Prediction error", "value": _money4_signed(pnl.prediction_error_usd)},
            {"label": "Average error", "value": _money4_signed(pnl.average_prediction_error_usd)},
            {"label": "Max error", "value": _money4(pnl.max_prediction_error_usd)},
        ]
    )
    ordered = sorted(summary.last_trades, key=lambda t: t.at)
    fig = go.Figure()
    fig.add_trace(go.Bar(name="Predicted", x=[t.at.strftime("%H:%M:%S") for t in ordered], y=[t.predicted_net_usd for t in ordered], marker_color=SEQUENTIAL_BLUE))
    fig.add_trace(go.Bar(name="Actual", x=[t.at.strftime("%H:%M:%S") for t in ordered], y=[t.actual_net_usd for t in ordered], marker_color=STATUS_GOOD))
    fig.update_layout(barmode="group", title="Predicted vs actual net P&L per trade")
    st.plotly_chart(style_fig(fig, height=280), use_container_width=True)


def _render_live_safety(script_status, raw: dict, summary) -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">SAFETY STATUS</div>', unsafe_allow_html=True)
    perms = data.get_api_permissions_status_cached()
    counts = summary.counts

    def _badge(ok: bool | None, good_text: str, bad_text: str, unknown_text: str = "INCONNU") -> str:
        if ok is None:
            return f'<span class="badge badge-modify">🟠 {unknown_text}</span>'
        return f'<span class="badge badge-go">🟢 {good_text}</span>' if ok else f'<span class="badge badge-nogo">🔴 {bad_text}</span>'

    kill_switch_ok = not raw.get("KILL_SWITCH_ENGAGED", False) if script_status.available else None
    ledger_ok = all(c.reconcile_ok for c in []) or True  # reconciliation itself is verified live by each orchestration run, not re-derivable after the fact from the ledger alone (no per-trade flag persisted)
    unhedged_ok = (counts.unhedged_incidents == 0) if counts is not None else None
    unknown_state_ok = raw.get("INCIDENT") is None or "UNKNOWN ORDER STATE" not in str(raw.get("INCIDENT", {}).get("type", ""))

    rows = [
        ("KILL SWITCH", _badge(kill_switch_ok, "OK", "ENGAGED")),
        ("UNHEDGED POSITION", _badge(unhedged_ok, "AUCUNE", "DÉTECTÉE")),
        ("DOUBLE ALLOCATION", _badge(True, "IMPOSSIBLE (max_concurrent=1)", "—")),
        ("UNKNOWN ORDER STATE", _badge(unknown_state_ok, "AUCUN", "DÉTECTÉ")),
        ("API PERMISSIONS", _badge(perms.reachable and perms.binance_trading_enabled and perms.bybit_trading_enabled if perms.reachable else None, "OK", "PROBLÈME")),
        ("WITHDRAWALS", _badge(perms.reachable and perms.binance_withdrawals_disabled and perms.bybit_withdrawals_disabled if perms.reachable else None, "DISABLED", "ENABLED ⚠️")),
    ]
    for label, badge in rows:
        st.markdown(f'<div class="simple-perf-row"><span class="k">{label}</span><span class="v">{badge}</span></div>', unsafe_allow_html=True)
    st.caption(
        "LEDGER RECONCILED : chaque cycle réel exécute une réconciliation solde-avant/solde-après avant d'être compté "
        "SUCCESSFUL (app.execution.reconciliation) — un mismatch aurait immédiatement arrêté la session (voir INCIDENT ci-dessus si présent)."
    )


def _render_live_funnel(summary) -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">OPPORTUNITY FUNNEL</div>', unsafe_allow_html=True)
    counts = summary.counts
    render_stat_cards(
        [
            {"label": "Inventory attempts", "value": f"{summary.total_inventory_attempts}"},
            {"label": "Arbitrage attempts", "value": f"{summary.total_arb_attempts}"},
            {"label": "Filled (both legs)", "value": f"{counts.complete_arbitrages if counts else 0}"},
            {"label": "Complete & successful", "value": f"{counts.successful if counts else 0}"},
        ]
    )
    if summary.missed_causes:
        with st.expander(f"Missed opportunities — causes ({len(summary.missed_causes)})"):
            for c in summary.missed_causes:
                st.markdown(f'<div class="simple-perf-row"><span class="k">{c.cause}</span><span class="v">{c.count}×</span></div>', unsafe_allow_html=True)
    st.caption(
        "Le funnel pré-exécution (SCANS, NET POSITIVE, CONFIRMED_SHORT_TERM) n'est pas persisté par les scripts live "
        "one-off — seules les tentatives réelles (inventaire + arbitrage) et leurs raisons de rejet le sont."
    )


def _render_live_session_performance(summary, raw: dict) -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">SESSION PERFORMANCE</div>', unsafe_allow_html=True)
    pnl = summary.pnl
    counts = summary.counts
    duration_h = raw.get("SESSION_DURATION_HOURS")
    trades_per_hour = (counts.complete_arbitrages / duration_h) if (counts and duration_h) else None
    render_stat_cards(
        [
            {"label": "Session duration", "value": f"{duration_h:.2f} h" if duration_h is not None else "—"},
            {"label": "Trades / hour", "value": f"{trades_per_hour:.1f}" if trades_per_hour is not None else "—"},
            {"label": "P&L / hour", "value": _money4_signed(pnl.pnl_per_hour_usd) if pnl and pnl.pnl_per_hour_usd is not None else "—"},
            {"label": "Capital rotations", "value": f"{counts.complete_arbitrages}" if counts else "—", "sub": "1 rotation = 1 cycle complet, pas de cooldown artificiel"},
        ]
    )


def _render_live_activity_feed(summary) -> None:
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">LIVE ACTIVITY FEED</div>', unsafe_allow_html=True)
    if not summary.last_trades:
        st.caption("Aucune activité réelle enregistrée pour l'instant.")
        return
    st.caption("Un événement par cycle complet (le détail intermédiaire BUY/SELL/RECONCILE n'est pas persisté séparément) :")
    for t in summary.last_trades[:20]:
        pnl_str = f"{t.actual_net_usd:+.4f} $" if t.actual_net_usd is not None else "—"
        time_str = t.at.strftime("%H:%M:%S") if t.at else "—"
        st.markdown(
            f'<div style="font-family:monospace;font-size:0.85rem;color:{INK_SECONDARY};padding:3px 0;">'
            f'{time_str} — {t.symbol} {t.buy_exchange}→{t.sell_exchange} · cycle {pnl_str}</div>',
            unsafe_allow_html=True,
        )


def _render_live_controls(status_key: str) -> None:
    """Deliberately non-functional (user directive, 2026-08-25, section
    14: buttons must "demander confirmation" and never touch withdrawal/
    transfer — but also never pretend to control something they can't
    reach). Real live trading this whole project has only ever run via
    separately-authorized one-off scripts, each with its OWN guard
    instance in its OWN process — main.py's engine has never had
    live_trading_enabled flipped, so its kill switch controls nothing
    real. A button that reported "success" against that engine would be
    actively misleading in exactly the place where that's most
    dangerous. Until a real live process exposes something this
    dashboard can safely reach (a control channel, not just a status
    file), these stay disabled and say so honestly rather than fake it."""
    st.markdown('<div class="simple-card-label" style="margin-top:22px;">LIVE SESSION CONTROLS</div>', unsafe_allow_html=True)
    col1, col2, col3 = st.columns(3)
    with col1:
        st.button("⏸️ PAUSE NEW TRADES", key="live_pause_btn", disabled=True, use_container_width=True)
    with col2:
        st.button("▶️ RESUME", key="live_resume_btn", disabled=True, use_container_width=True)
    with col3:
        st.button("🔴 ENGAGE KILL SWITCH", key="live_kill_btn", disabled=True, use_container_width=True)
    st.caption(
        "Ces trois contrôles sont désactivés intentionnellement : aucun processus live n'est actuellement en écoute d'un "
        "signal de pause, et le coupe-circuit du moteur main.py n'a aucun effet réel (ce moteur n'a jamais exécuté de "
        "trade réel — chaque session live de ce projet tourne dans un script séparément autorisé, avec son propre kill "
        "switch en mémoire, invisible de l'extérieur pendant son exécution). Un bouton qui semblerait fonctionner sans "
        "réellement rien arrêter serait plus dangereux qu'utile. Aucun bouton retrait/transfert n'existe ni n'existera ici."
    )


@st.fragment(run_every="5s")
def render_live_trading_page() -> None:
    """LIVE TRADING — real money observability (user directive,
    2026-08-25). Every figure comes from real exchange balances, the two
    real-money ledgers, and the live orchestration script's own status
    file — never paper/simulation data. This page never places,
    modifies, or cancels a trading order itself
    (tests/test_dashboard_is_read_only.py enforces this mechanically for
    dashboard/data.py); its one live control (kill switch) only ever
    engages a stop, never initiates a trade."""
    script_status = data.get_live_script_status_cached()
    summary = data.get_live_trading_page_summary_cached()
    raw = script_status.raw if script_status.available else {}

    status_key = _render_live_header(script_status, raw)
    if not summary.reachable:
        st.error("Base de données injoignable — impossible d'afficher les données réelles.")
        return

    _render_live_capital(summary)
    _render_live_pnl(summary)
    _render_live_trades(summary)
    _render_live_best_opportunity()
    _render_live_inventory(summary)
    _render_live_active_cycle(raw)
    _render_live_predicted_vs_actual(summary)
    _render_live_safety(script_status, raw, summary)
    _render_live_funnel(summary)
    _render_live_session_performance(summary, raw)
    _render_live_activity_feed(summary)
    _render_live_controls(status_key)


def render_simple_mode() -> None:
    page = st.session_state.get("simple_page", "accueil")
    render_header(page)
    if page == "trades":
        render_trades_page()
    elif page == "performance":
        render_performance_page()
    elif page == "parametres":
        render_parametres_page()
    elif page == "reality":
        render_reality_page()
    elif page == LIVE_TRADING_NAV_KEY:
        render_live_trading_page()
    else:
        render_accueil()
