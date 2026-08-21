"""Shared design tokens, CSS, and small render helpers for both Simple and Expert mode."""

from datetime import UTC, datetime

import plotly.graph_objects as go
import streamlit as st
import streamlit.components.v1 as components

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

# Simple Mode's plain-language names for the same strategies (spec section 17
# — never show a trader term where a plain description works just as well).
STRATEGY_LABELS_SIMPLE = {
    "stablecoin": "Stablecoins",
    "cross_exchange": "Entre plateformes",
    "triangular": "Triangulaire",
    "funding": "Spot ↔ Perpetual",
    "basis": "Spot ↔ Future",
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

# Continuous Execution spec, section 43 — plain-language labels for
# app.execution.validator.RejectionReason values.
REJECTION_REASON_LABELS = {
    "stale_data": "Données trop anciennes",
    "fees_too_high": "Frais trop élevés",
    "edge_too_low": "Écart trop faible",
    "position_already_open": "Déjà en position",
    "holding_too_long": "Durée de détention trop longue (> 20 min)",
}

# TradeStatus values (app.simulation.paper_trader) — trade-attempt
# granularity, distinct from REJECTION_REASON_LABELS above (which is
# opportunity-level, from app.execution.validator). This is what actually
# answers "the engine tried but couldn't take it": NO_CAPITAL_AVAILABLE and
# MAX_CONCURRENT_POSITIONS have no opportunity-level equivalent — an
# approved opportunity is always attempted against every portfolio
# regardless of any single one's capital state.
ATTEMPT_OUTCOME_LABELS = {
    "simulated_executed": "Exécuté",
    "partial_fill": "Exécuté (remplissage partiel)",
    "missed": "Manqué — jambe maker non remplie à temps",
    "edge_disappeared": "Écart disparu avant exécution (latence)",
    "simulated_failed": "Échoué — données déjà obsolètes au moment du pricing",
    "no_capital_available": "Capital indisponible sur ce portefeuille",
    "max_concurrent_positions": "Limite de positions simultanées atteinte",
    "emergency_unwind": "Débouclage d'urgence (une jambe a échoué)",
    "time_stop_exit": "Sorti par le stop de durée (30 min)",
}

ILLUSTRATIVE_CAPITAL_USD = 1_000


def inject_css() -> None:
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

        /* --- Simple Mode --- */
        .simple-topbar {{
            display: flex;
            align-items: center;
            justify-content: space-between;
            flex-wrap: wrap;
            gap: 10px;
            margin-bottom: 4px;
        }}
        .simple-brand {{ font-size: 1.15rem; font-weight: 700; letter-spacing: 0.02em; color: {INK_PRIMARY}; }}
        .simple-status-pill {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 5px 14px;
            border-radius: 999px;
            font-size: 0.88rem;
            font-weight: 650;
        }}
        .simple-status-running {{ background: rgba(12,163,12,0.16); color: #4ade80; }}
        .simple-status-degraded {{ background: rgba(250,178,25,0.16); color: #fbbf24; }}
        .simple-status-down {{ background: rgba(208,59,59,0.16); color: #f87171; }}
        .simple-exchanges {{ color: {INK_MUTED}; font-size: 0.82rem; margin-top: 4px; }}
        .simple-exchanges b {{ color: {INK_SECONDARY}; font-weight: 500; }}

        .simple-sim-badge {{
            display: inline-block;
            padding: 3px 12px;
            border-radius: 999px;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            background: rgba(57,135,229,0.16);
            color: #7fb2f0;
            margin-bottom: 14px;
        }}
        .simple-live-badge {{
            display: inline-block;
            padding: 3px 12px;
            border-radius: 999px;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.06em;
            background: rgba(208,59,59,0.22);
            color: #f87171;
            margin-bottom: 14px;
        }}

        .simple-card {{
            background: {SURFACE};
            border: 1px solid {BORDER};
            border-radius: 18px;
            padding: 22px 24px;
            margin-bottom: 14px;
        }}
        .simple-card-label {{
            color: {INK_MUTED};
            font-size: 0.8rem;
            font-weight: 700;
            text-transform: uppercase;
            letter-spacing: 0.06em;
            margin-bottom: 10px;
        }}
        .simple-card-figure {{
            color: {INK_PRIMARY};
            font-size: 2.4rem;
            font-weight: 700;
            line-height: 1.1;
        }}
        .simple-card-sub {{ color: {INK_SECONDARY}; font-size: 0.95rem; margin-top: 6px; }}
        .simple-card-sub.good {{ color: {STATUS_GOOD}; }}
        .simple-card-sub.bad {{ color: {STATUS_CRITICAL}; }}
        .simple-card-sub.warn {{ color: {STATUS_WARNING}; }}
        .simple-card-figure.good {{ color: #4ade80; }}
        .simple-card-figure.bad {{ color: #f87171; }}
        .simple-card-figure.warn {{ color: {STATUS_WARNING}; }}

        .simple-grid-2 {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
            gap: 14px;
            margin-bottom: 14px;
        }}

        .simple-state-card {{
            border-radius: 18px;
            padding: 22px 24px;
            margin-bottom: 14px;
            border: 1px solid {BORDER};
        }}
        .simple-state-card.good {{ background: rgba(12,163,12,0.09); border-color: rgba(12,163,12,0.30); }}
        .simple-state-card.warn {{ background: rgba(250,178,25,0.08); border-color: rgba(250,178,25,0.28); }}
        .simple-state-card.bad {{ background: rgba(208,59,59,0.09); border-color: rgba(208,59,59,0.30); }}
        .simple-state-title {{ font-size: 1.1rem; font-weight: 700; color: {INK_PRIMARY}; margin-bottom: 6px; }}
        .simple-state-body {{ color: {INK_SECONDARY}; font-size: 0.92rem; line-height: 1.5; }}

        .simple-opp-symbol {{ font-size: 1.5rem; font-weight: 700; color: {INK_PRIMARY}; }}
        .simple-opp-route {{ color: {INK_MUTED}; font-size: 0.88rem; margin-bottom: 14px; }}
        .simple-opp-row {{ display: flex; justify-content: space-between; padding: 5px 0; font-size: 0.94rem; }}
        .simple-opp-row .k {{ color: {INK_MUTED}; }}
        .simple-opp-row .v {{ color: {INK_PRIMARY}; font-weight: 600; }}

        .simple-perf-row {{ display: flex; justify-content: space-between; padding: 8px 0; font-size: 0.95rem; border-bottom: 1px solid {GRIDLINE}; }}
        .simple-perf-row:last-child {{ border-bottom: none; }}
        .simple-perf-row .k {{ color: {INK_SECONDARY}; }}
        .simple-perf-row .v {{ font-weight: 650; }}

        .simple-nav {{
            display: flex;
            gap: 6px;
            margin: 14px 0 18px 0;
            flex-wrap: wrap;
        }}

        .simple-trade-row {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 16px;
            border-bottom: 1px solid {GRIDLINE};
            font-size: 0.92rem;
        }}
        .simple-trade-row:last-child {{ border-bottom: none; }}
        .simple-trade-time {{ color: {INK_MUTED}; width: 64px; flex-shrink: 0; }}
        .simple-trade-symbol {{ color: {INK_PRIMARY}; font-weight: 600; flex: 1; }}
        .simple-trade-result {{ font-weight: 700; text-align: right; margin-right: 14px; }}
        .simple-trade-status {{ font-size: 0.8rem; padding: 2px 10px; border-radius: 999px; }}
        .simple-trade-status.won {{ background: rgba(12,163,12,0.16); color: #4ade80; }}
        .simple-trade-status.lost {{ background: rgba(208,59,59,0.16); color: #f87171; }}

        section[data-testid="stSidebar"] {{ display: none; }}

        /* --- Live Dashboard (Reality Engine addendum) --- */
        .simple-connection-badge {{
            display: inline-flex;
            align-items: center;
            gap: 6px;
            padding: 3px 12px;
            border-radius: 999px;
            font-size: 0.72rem;
            font-weight: 700;
            letter-spacing: 0.04em;
            margin-left: 8px;
        }}
        .simple-connection-live {{ background: rgba(12,163,12,0.16); color: #4ade80; }}
        .simple-connection-reconnecting {{ background: rgba(250,178,25,0.16); color: #fbbf24; }}
        .simple-connection-down {{ background: rgba(208,59,59,0.16); color: #f87171; }}

        /* A soft, slow wash — not a ring that pops in instantly — so a
        value update reads as "this just settled" rather than a flash. */
        @keyframes pulseGood {{
            0% {{ background: rgba(12,163,12,0.16); border-color: rgba(12,163,12,0.32); }}
            100% {{ background: {SURFACE}; border-color: {BORDER}; }}
        }}
        @keyframes pulseBad {{
            0% {{ background: rgba(208,59,59,0.14); border-color: rgba(208,59,59,0.30); }}
            100% {{ background: {SURFACE}; border-color: {BORDER}; }}
        }}
        .simple-card {{ transition: background 1.8s ease, border-color 1.8s ease; }}
        .simple-card.pulse-good {{ animation: pulseGood 2.2s ease-out 1; }}
        .simple-card.pulse-bad {{ animation: pulseBad 2.2s ease-out 1; }}
        .simple-card-figure, .simple-card-sub {{ transition: color 0.5s ease; }}

        .simple-pnl-split-row {{ display: flex; justify-content: space-between; padding: 6px 0; font-size: 0.92rem; }}
        .simple-pnl-split-row .k {{ color: {INK_MUTED}; }}
        .simple-pnl-split-row .v {{ font-weight: 650; }}
        .simple-pnl-split-total {{ border-top: 1px solid {GRIDLINE}; margin-top: 4px; padding-top: 10px; font-size: 1.05rem; }}

        .simple-event-row {{
            display: flex;
            gap: 12px;
            padding: 7px 0;
            font-size: 0.86rem;
            border-bottom: 1px solid {GRIDLINE};
        }}
        .simple-event-row:last-child {{ border-bottom: none; }}
        .simple-event-time {{ color: {INK_MUTED}; width: 58px; flex-shrink: 0; }}
        .simple-event-text {{ color: {INK_SECONDARY}; flex: 1; }}
        .simple-event-text b {{ color: {INK_PRIMARY}; }}
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


def render_live_number_card(label: str, rows: list[dict], *, key: str) -> None:
    """A stat card whose numbers count smoothly from their previous value
    to the new one, instead of the whole card re-rendering (Live Dashboard
    addendum, user feedback: "les cartes doivent rester intactes, seuls les
    chiffres s'actualisent, animation fluide, pas toute la carte").

    Plain `st.markdown` text can't be tweened — there's no CSS mechanism to
    animate arbitrary text content — so this renders one small,
    self-contained HTML/JS component (an iframe, via
    st.components.v1.html) per card. The label, background, and border are
    baked into the SAME static HTML string every render (identical byte-
    for-byte when nothing changed), so they never visibly change; only the
    JS-driven `<span>` for each number moves, via requestAnimationFrame.

    `rows`: each dict —
      value: float (required)
      decimals: int (default 2)
      big: bool — the large hero figure (one row should set this) vs a
           smaller sub-row (default False)
      signed: bool — prepend "+" for a positive value, matching Python's
              `:+.2f}` convention used everywhere else in this dashboard
              (default False)
      suffix: str, e.g. " $" or " %" (default "")
      color: str hex/css color for this row's text (default INK_PRIMARY)
      label: str | None — a small inline label before the number, e.g.
             "Disponible maintenant :" (default None)
      sub_text: str | None — small static text rendered under this row
             (e.g. "12 gagnants · 3 perdants"). Not tweened — it's not a
             single scalar — but still lives inside the same persistent
             card, so it never causes the whole-card flash a separate
             st.markdown call would (default None)
    """
    state_prefix = f"_live_prev_card_{key}_"
    spans = []
    scripts = []
    height = 68  # card padding + label line
    for i, row in enumerate(rows):
        row_id = f"livecard_{key}_{i}"
        value = row["value"]
        previous = st.session_state.get(state_prefix + str(i), value)
        st.session_state[state_prefix + str(i)] = value
        big = row.get("big", False)
        font_size = "2.4rem" if big else "0.95rem"
        font_weight = "700" if big else "650"
        color = row.get("color", INK_PRIMARY)
        margin_top = "0px" if i == 0 else "6px"
        prefix_label = f'<span style="color:{INK_SECONDARY};font-weight:500;">{row["label"]}</span> ' if row.get("label") else ""
        spans.append(
            f'<div style="margin-top:{margin_top};font-size:{font_size};font-weight:{font_weight};'
            f'color:{color};line-height:1.2;font-family:{FONT_STACK};">'
            f'{prefix_label}<span id="{row_id}"></span></div>'
        )
        if row.get("sub_text"):
            spans.append(f'<div style="margin-top:4px;font-size:0.88rem;color:{INK_SECONDARY};font-family:{FONT_STACK};">{row["sub_text"]}</div>')
            height += 20
        signed = "true" if row.get("signed", False) else "false"
        scripts.append(
            f'tweenNumber(document.getElementById("{row_id}"), {previous!r}, {value!r}, 700, '
            f'{row.get("decimals", 2)}, {signed}, {row.get("suffix", "")!r});'
        )
        height += 40 if big else 24

    html = f"""
    <div style="font-family:{FONT_STACK};background:{SURFACE};border:1px solid {BORDER};
    border-radius:18px;padding:22px 24px;box-sizing:border-box;">
      <div style="color:{INK_MUTED};font-size:0.8rem;font-weight:700;text-transform:uppercase;
      letter-spacing:0.06em;margin-bottom:10px;">{label}</div>
      {"".join(spans)}
    </div>
    <script>
    function formatThousands(value, decimals) {{
        let text = value.toFixed(decimals);
        let neg = text.startsWith("-");
        if (neg) text = text.slice(1);
        let [intPart, decPart] = text.split(".");
        intPart = intPart.replace(/\\B(?=(\\d{{3}})+(?!\\d))/g, " ");
        return (neg ? "-" : "") + intPart + (decPart !== undefined ? "." + decPart : "");
    }}
    function tweenNumber(el, start, end, duration, decimals, signed, suffix) {{
        if (!el) return;
        const startTime = performance.now();
        function ease(t) {{ return 1 - Math.pow(1 - t, 3); }}
        function frame(now) {{
            const t = Math.min(1, (now - startTime) / duration);
            const current = start + (end - start) * ease(t);
            let text = formatThousands(current, decimals);
            if (signed && current >= 0) text = "+" + text;
            el.textContent = text + suffix;
            if (t < 1) requestAnimationFrame(frame);
        }}
        requestAnimationFrame(frame);
    }}
    {"".join(scripts)}
    </script>
    """
    components.html(html, height=height)


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


def humanize_delta(at: datetime) -> str:
    if at.tzinfo is None:
        at = at.replace(tzinfo=UTC)
    seconds = max(0, (datetime.now(UTC) - at).total_seconds())
    if seconds < 60:
        return f"il y a {int(seconds)} s"
    if seconds < 3600:
        return f"il y a {int(seconds // 60)} min"
    return f"il y a {int(seconds // 3600)} h"
