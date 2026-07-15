"""Refi Intelligence Desk — Streamlit dashboard (thin presentation layer).

All numbers come from src/core and src/app.data_access; this file holds zero rule
logic, economics, or SQL. Streamlit calls are confined to main() and the render_*
helpers so the module imports without side effects. Run:

    streamlit run src/app/dashboard.py
"""

from __future__ import annotations

import plotly.graph_objects as go
import streamlit as st

from src.app import bootstrap, data_access as da
from src.core import ladder, pool

# Design language (CC-BRIEF-04).
BRASS = "#D4A94E"
LEDGER = "#101915"
PANEL = "#1f2d25"
TEXT = "#E9E4D6"
OPPORTUNITY = "#6BBF8A"  # rates falling
TREASURY_HUE = "#7A8B82"  # subdued context

INGEST_CMD = "python -m src.data.ingest --backfill-years 5"
SECRETS_HINT = (
    "On Streamlit Community Cloud, add FRED_API_KEY under Advanced settings -> Secrets "
    "(see .streamlit/secrets.toml.example)."
)

_METRIC_SPEC = [
    (da.TREASURY, "10-Yr Treasury"),
    (da.VA_INDEX, "30-Yr VA"),
    (da.FHA_INDEX, "30-Yr FHA"),
    (da.CONFORMING_INDEX, "30-Yr Conforming"),
]


# --- Cached loaders (keyed on as-of date + assumptions for correct invalidation) ---

@st.cache_data(show_spinner=False)
def _metrics(as_of: str) -> dict:
    conn = da.connect()
    try:
        out = {}
        for sid, label in _METRIC_SPEC:
            latest = da.get_latest(conn, sid)
            out[sid] = (label, latest[1] if latest else None, da.delta_vs_prior(conn, sid, 7))
        return out
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def _chart_data(n_days: int, as_of: str) -> dict:
    conn = da.connect()
    try:
        return {sid: da.get_range(conn, sid, n_days) for sid in (da.TREASURY, da.VA_INDEX, da.FHA_INDEX)}
    finally:
        conn.close()


@st.cache_data(show_spinner=False)
def _ladder(cost_pct: float, threshold: int, seed: int, as_of: str):
    conn = da.connect()
    try:
        loans = pool.load_pool(conn) if seed == pool.DEFAULT_SEED else []
        if not loans:
            loans = pool.generate_pool(seed)
        return ladder.build_ladder(conn=conn, loans=loans, cost_pct=cost_pct, threshold_months=threshold)
    finally:
        conn.close()


# --- Render helpers ---

def render_masthead(as_of: str | None) -> None:
    st.markdown(
        f"<h1 style='color:{BRASS};font-family:monospace;letter-spacing:3px;margin-bottom:0'>"
        "REFI INTELLIGENCE DESK</h1>",
        unsafe_allow_html=True,
    )
    st.caption("VA IRRRL · FHA Streamline opportunity monitor — public data, modeled pools")
    if as_of:
        st.caption(f"As of {as_of} (latest observation in the data)")


def render_metrics(as_of: str) -> None:
    cols = st.columns(4)
    for col, (sid, (label, value, delta)) in zip(cols, _metrics(as_of).items()):
        col.metric(
            label,
            f"{value:.3f}%" if value is not None else "—",
            f"{delta:+.3f}" if delta is not None else None,
            delta_color="inverse",  # a falling rate (negative delta) reads as green
        )


def render_chart(as_of: str, selected_trigger: float) -> None:
    st.subheader("Rate trends")
    n_days = st.radio("Window", [90, 180, 365], horizontal=True, format_func=lambda d: f"{d}d", index=0)
    data = _chart_data(n_days, as_of)
    fig = go.Figure()
    for sid, label, color, width in [
        (da.TREASURY, "10-Yr Treasury", TREASURY_HUE, 1.3),
        (da.VA_INDEX, "30-Yr VA", BRASS, 2.4),
        (da.FHA_INDEX, "30-Yr FHA", OPPORTUNITY, 2.4),
    ]:
        rows = data.get(sid, [])
        if rows:
            fig.add_trace(go.Scatter(
                x=[d for d, _ in rows], y=[v for _, v in rows], name=label,
                line=dict(color=color, width=width),
                hovertemplate="%{x}<br>" + label + ": %{y:.3f}%<extra></extra>",
            ))
    fig.add_hline(y=selected_trigger, line_dash="dash", line_color=BRASS,
                  annotation_text=f"trigger {selected_trigger:.3f}%", annotation_font_color=BRASS)
    fig.update_layout(paper_bgcolor=LEDGER, plot_bgcolor=LEDGER, height=360,
                      font=dict(color=TEXT, family="monospace"), margin=dict(l=10, r=10, t=10, b=10),
                      legend=dict(orientation="h", y=1.08))
    fig.update_xaxes(gridcolor=PANEL)
    fig.update_yaxes(gridcolor=PANEL, ticksuffix="%")
    st.plotly_chart(fig, use_container_width=True)


def _fmt(value: float | None) -> str:
    return f"{value:.1f}" if value is not None else "—"


def render_ladder(rungs: list[ladder.LadderRung]) -> ladder.LadderRung:
    st.subheader("Trigger ladder")
    st.caption("How many modeled loans clear agency + economic tests as the rate steps down.")
    triggers = [r.trigger_rate for r in rungs]
    if st.session_state.get("selected_trigger") not in triggers:
        st.session_state["selected_trigger"] = triggers[len(triggers) // 2]
    st.select_slider("Trigger rate", options=triggers, key="selected_trigger",
                     format_func=lambda t: f"{t:.3f}%")
    selected = st.session_state["selected_trigger"]

    max_cum = max((r.cumulative_count for r in rungs), default=1) or 1
    head = ("<tr style='text-align:right;color:#9fb3a6'><th>trigger</th><th>dist</th>"
            "<th>+new</th><th>cumul</th><th>med recoup</th><th>med BE</th>"
            "<th style='width:28%'>shape</th></tr>")
    body = []
    for r in rungs:
        is_sel = r.trigger_rate == selected
        bar = (f"<div style='background:{BRASS if is_sel else OPPORTUNITY};height:11px;"
               f"width:{int(r.cumulative_count / max_cum * 100)}%'></div>")
        bg = f"background:{PANEL};" if is_sel else ""
        body.append(
            f"<tr style='text-align:right;{bg}'><td>{r.trigger_rate:.3f}</td>"
            f"<td>{r.distance_from_market:.3f}</td><td>+{r.newly_eligible}</td>"
            f"<td>{r.cumulative_count:,}</td><td>{_fmt(r.median_statutory_recoupment)}</td>"
            f"<td>{_fmt(r.median_break_even)}</td><td>{bar}</td></tr>"
        )
    st.markdown(
        f"<table style='width:100%;font-family:monospace;font-size:0.9em'>{head}{''.join(body)}</table>",
        unsafe_allow_html=True,
    )
    rung = next(r for r in rungs if r.trigger_rate == selected)
    render_rung_detail(rung)
    return rung


def render_rung_detail(rung: ladder.LadderRung) -> None:
    st.markdown(
        f"**At {rung.trigger_rate:.3f}%**, {rung.cumulative_count:,} modeled loans clear both "
        f"agency and economic tests; median recoupment {_fmt(rung.median_statutory_recoupment)} months."
    )
    c1, c2, c3, c4 = st.columns(4)
    c1.metric("VA eligible", f"{rung.eligible_va:,}")
    c2.metric("FHA eligible", f"{rung.eligible_fha:,}")
    c3.metric("Soft-blocker flags", f"{rung.soft_blocker_count:,}")
    c4.metric("Unknown flags", f"{rung.unknown_count:,}")
    st.caption("Call-clear flags are surfaced alongside and never reduce the eligible count.")


def render_sidebar() -> tuple[float, int, int]:
    st.sidebar.header("Assumptions")
    cost = st.sidebar.slider("Recoupment-eligible cost %", 0.5, 1.5, 1.0, 0.1,
                             help="Synthetic assumption — not market truth.")
    threshold = st.sidebar.number_input("Break-even threshold (months)", 12, 120, 48, 1,
                                        help="House policy, not an agency rule.")
    seed = st.sidebar.number_input("Pool seed", value=pool.DEFAULT_SEED, step=1)
    if st.sidebar.button("Regenerate pool"):
        st.cache_data.clear()
    return cost / 100.0, int(threshold), int(seed)


def render_footer() -> None:
    st.divider()
    st.caption("Data sources: FRED (Federal Reserve), Freddie Mac PMMS, and Optimal Blue rate "
               "indices via FRED.")
    st.caption("Synthetic/modeled loan pool — no real borrower data. Not financial advice. "
               "No NMLS-regulated activity occurs in this software. Educational portfolio project.")


def render_morning_brief(
    as_of: str,
    cost_pct: float,
    threshold: int,
    seed: int,
    selected_trigger: float,
) -> None:
    """AI morning brief with eval gate — template fallback when no API key."""
    from evals.verify import verify_brief
    from src.brief.generate import generate_brief
    from src.brief.snapshot import build_snapshot

    st.subheader("Morning brief")
    conn = da.connect()
    try:
        snapshot = build_snapshot(
            conn,
            cost_pct=cost_pct,
            threshold_months=threshold,
            seed=seed,
            selected_trigger=selected_trigger,
        )
        brief, source = generate_brief(snapshot, mode="auto")
        result = verify_brief(brief, snapshot)
    finally:
        conn.close()

    badge = "Eval PASS" if result.passed else "Eval FAIL"
    st.caption(f"Source: {source} | {badge} | {result.summary()}")
    if result.errors:
        for err in result.errors:
            st.error(err)
    for warn in result.warnings:
        st.warning(warn)
    st.markdown(brief)


def main() -> None:
    st.set_page_config(page_title="Refi Intelligence Desk", layout="wide")
    bootstrap.apply_streamlit_secrets()
    with st.spinner("Loading rate data…"):
        ready, as_of = bootstrap.ensure_database()

    render_masthead(as_of)
    if not ready:
        st.error("No rate data found in the database. Run the ingest command first:")
        st.code(INGEST_CMD, language="bash")
        st.info(SECRETS_HINT)
        st.stop()

    cost_pct, threshold, seed = render_sidebar()
    rungs, _current_va, _current_fha = _ladder(cost_pct, threshold, seed, as_of)
    if st.session_state.get("selected_trigger") not in [r.trigger_rate for r in rungs]:
        st.session_state["selected_trigger"] = rungs[len(rungs) // 2].trigger_rate

    render_metrics(as_of)
    render_chart(as_of, st.session_state["selected_trigger"])
    selected_rung = render_ladder(rungs)
    render_morning_brief(
        as_of,
        cost_pct,
        threshold,
        seed,
        selected_rung.trigger_rate,
    )
    render_footer()


if __name__ == "__main__":
    main()
