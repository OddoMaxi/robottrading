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
    STRATEGY_LABELS_SIMPLE,
    humanize_delta,
    render_live_number_card,
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

NAV_PAGES = [("accueil", "Accueil"), ("trades", "Trades"), ("performance", "Performance"), ("reality", "Reality"), ("parametres", "Paramètres")]


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
def render_live_status_row() -> None:
    """Live Dashboard addendum, sections 10-11 — the robot status pill and
    connection indicator update on their own every 3s, independent of the
    nav bar below (which only changes on an explicit click). No manual
    reconnect is needed: a fragment that keeps auto-rerunning *is* the
    reconnect loop — the moment fresh data is available again, the next
    tick picks it up."""
    robot = data.get_robot_status_cached()
    status_class = {"running": "simple-status-running", "degraded": "simple-status-degraded", "down": "simple-status-down"}[robot.health.value]
    status_label = {"running": "🟢 EN MARCHE", "degraded": "🟡 SURVEILLANCE", "down": "🔴 PROBLÈME"}[robot.health.value]
    connection_class, connection_label = CONNECTION_BADGE[robot.health.value]
    exchange_bits = " &nbsp;·&nbsp; ".join(
        f'<b>{name.capitalize()}</b> {"✓" if ok else "✕"}' for name, ok in robot.exchanges_connected.items()
    )

    st.markdown(
        f'<div class="simple-topbar"><div class="simple-brand">🤖 ROBOT</div>'
        f'<div class="simple-status-pill {status_class}">{status_label}</div>'
        f'<span class="simple-connection-badge {connection_class}">{connection_label}</span></div>'
        f'<div class="simple-exchanges">{exchange_bits}</div>'
        '<div><span class="simple-sim-badge">MODE SIMULATION</span></div>',
        unsafe_allow_html=True,
    )


def render_header(active_page: str) -> None:
    render_live_status_row()

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
        render_stat_cards(
            [
                {"label": "Détections DEX brutes (depuis audit)", "value": f"{dup.raw_detections:,}".replace(",", " ")},
                {"label": "Doublons économiques éliminés", "value": f"{dup.duplicate_economic_events_eliminated:,}".replace(",", " ")},
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
                f'<span style="color:{INK_MUTED};">({t.n_filled} filled, {t.n_no_capital_available} sans capital)</span></span>'
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
    else:
        render_accueil()
