"""Streamlit dashboard entrypoint (Dashboard Simple V4 spec).

Simple Mode is the default — a handful of plain-language cards a
non-trader can read in under 5 seconds. Mode Expert keeps every existing
technical view (net edge, gross spread, maker/taker, VWAP, orderbook,
latency, slippage, funding, opportunity score, execution probability,
strategy analytics) untouched, one click away.

Run with: streamlit run dashboard/app.py
"""

import sys
from pathlib import Path

import streamlit as st

# `streamlit run dashboard/app.py` puts dashboard/ on sys.path, not the repo
# root, so the top-level `app` package (and this file's own `dashboard.*`
# imports) need the repo root added explicitly.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dashboard.theme import inject_css

st.set_page_config(page_title="Robot d'arbitrage crypto", layout="wide", page_icon="🤖")
inject_css()

if "mode" not in st.session_state:
    st.session_state.mode = "simple"
if "simple_page" not in st.session_state:
    st.session_state.simple_page = "accueil"

if st.session_state.mode == "expert":
    from dashboard.expert import render_expert_mode

    if st.button("← Retour au mode simple"):
        st.session_state.mode = "simple"
        st.rerun()
    render_expert_mode()
else:
    from dashboard.simple import render_simple_mode

    render_simple_mode()
