"""
Ticket Trend Analysis: top issue drivers for SAC, B2B,
Order Fallout and SAT (Sales Assurance Team) over a day, a Sunday-Saturday
week or a custom range, weekly trends, and the SAL / SFS fixes behind each
driver.

Opened in a new browser tab from the dashboard's "Ticket Trend Analysis"
button. Data comes from the local cache kept by trend_store.py.
"""

import html
import textwrap
from datetime import date

import altair as alt
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import breach_logic
import config
import trend_analysis as ta
import trend_store
import ui_common as ui
import ui_table
from ui_common import (COLOR_CRITICAL, COLOR_GOOD, COLOR_WARNING, SURFACE_CARD, SURFACE_PANEL,
                       TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY, jira_url)

CATEGORY_COLOR = {"SAC": "#d95926", "B2B": "#199e70", "Order Fallout": "#c98500", "SAT": "#9085e9"}
# Fixed categorical order (dark-surface steps) for the top-5 driver lines.
SERIES_COLORS = ["#3987e5", "#d95926", "#199e70", "#c98500", "#d55181"]

SAMPLE_TICKETS = 5  # latest tickets of each driver shown as references
EXCEL_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
TOP_N_OPTIONS = {"Top 5": 5, "Top 10": 10, "Top 20": 20}  # drivers ranked per category; default config.TREND_TOP_N

FIX_STATE_COLOR = {
    "deployed": COLOR_GOOD, "resolved": COLOR_GOOD, "awaiting_release": "#3987e5",
    "in_progress": COLOR_WARNING, "not_started": TEXT_MUTED, "open": COLOR_WARNING,
}

st.set_page_config(page_title="Ticket Trend Analysis", page_icon="📈", layout="wide")
st_autorefresh(interval=5 * 60 * 1000, key="trend_autorefresh")

ui.apply_css(f"""
        .alert-box {{
            border-radius: 10px; padding: 8px 14px; margin: 4px 0; font-size: 0.82rem; color: {TEXT_PRIMARY};
            border: 1px solid; display: flex; gap: 10px; align-items: baseline;
        }}
        .alert-recurring {{ background: rgba(208,59,59,0.12); border-color: rgba(208,59,59,0.45); }}
        .alert-overdue {{ background: rgba(250,178,25,0.10); border-color: rgba(250,178,25,0.4); }}
        .alert-tag {{ font-size: 0.62rem; font-weight: 700; text-transform: uppercase; letter-spacing: 0.05em;
                      padding: 2px 8px; border-radius: 999px; white-space: nowrap; }}
        .alert-recurring .alert-tag {{ background: {COLOR_CRITICAL}; color: white; }}
        .alert-overdue .alert-tag {{ background: {COLOR_WARNING}; color: #1a1a1a; }}
        .driver-card {{
            border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; background: {SURFACE_PANEL};
            padding: 12px 16px; margin-bottom: 4px;
        }}
        .driver-head {{ display: flex; align-items: baseline; gap: 12px; }}
        .driver-rank {{ font-size: 0.8rem; font-weight: 700; color: {TEXT_MUTED}; }}
        .driver-name {{ font-size: 0.95rem; font-weight: 700; color: {TEXT_PRIMARY}; flex: 1; word-break: break-word; }}
        .driver-count {{ font-size: 1.15rem; font-weight: 700; color: {TEXT_PRIMARY}; white-space: nowrap; }}
        .driver-meta {{ font-size: 0.72rem; color: {TEXT_MUTED}; margin-top: 2px; }}
        .driver-summary {{ font-size: 0.8rem; color: {TEXT_SECONDARY}; margin: 8px 0 6px 0; line-height: 1.45; }}
        .fix-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 8px; font-size: 0.78rem;
                    color: {TEXT_SECONDARY}; padding: 5px 0; border-top: 1px solid rgba(255,255,255,0.06); }}
        .fix-proj {{ font-size: 0.62rem; font-weight: 700; color: {TEXT_MUTED}; width: 30px; }}
        .fix-key {{ color: #6ea8ff; font-weight: 700; text-decoration: none; }}
        .fix-key:hover {{ text-decoration: underline; }}
        .fix-chip {{ font-size: 0.66rem; font-weight: 700; padding: 1px 8px; border-radius: 999px; color: #111; white-space: nowrap; }}
        .fix-match {{ font-size: 0.66rem; color: {TEXT_MUTED}; border: 1px solid rgba(255,255,255,0.15);
                      padding: 0 6px; border-radius: 999px; }}
        .fix-summary {{ color: {TEXT_MUTED}; flex: 1; min-width: 200px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
        .fix-none {{ color: {TEXT_MUTED}; font-style: italic; }}
        .sample-row {{ display: flex; flex-wrap: wrap; align-items: center; gap: 6px; font-size: 0.74rem;
                       color: {TEXT_MUTED}; margin: 0 0 6px 0; }}
        .sample-key {{
            padding: 1px 8px; border-radius: 999px; background: rgba(110,168,255,0.12); color: #9cc3ff;
            font-weight: 600; text-decoration: none; white-space: nowrap;
        }}
        .sample-key:hover {{ background: rgba(110,168,255,0.25); color: {TEXT_PRIMARY}; }}
        .fix-alert {{ width: 100%; font-weight: 600; }}
        .fix-alert.recurring {{ color: #ff8a8a; }}
        .fix-alert.overdue {{ color: {COLOR_WARNING}; }}
""")


# One shared, read-only copy per server, latest sync only: st.cache_data would
# pickle ~100k rows and hand each visitor a copy (it ran out of memory).
@st.cache_resource(show_spinner=False, max_entries=1)
def load_data(sync_stamp):
    fixes = ta.load_fixes()
    return ta.load_tickets(), fixes, ta.fix_index(fixes)


head_r = ui.page_header(
    "Ticket Trend Analysis", "Top issue drivers for SAC, B2B, Order Fallout and SAT, with SAL / SFS fix tracking",
    '<path d="M3 17l6-6 4 4 8-8"/><path d="M15 7h6v6"/>')
with head_r:
    force = st.button("🔄  Sync with Jira now", width="stretch")

ui.ensure_fresh(force=force)
last_sync = trend_store.last_sync_time()
if last_sync is None:
    st.error("No ticket data yet: the Jira sync has not completed. Check the Jira credentials in .env and retry.")
    st.stop()

data_loader = ui.loading_if_new("trend_loaded_sync", last_sync.isoformat(),
                                "Loading ticket data&hellip; a year of tickets for SAC, B2B, Order Fallout and SAT")
tickets, fixes, fix_idx = load_data(last_sync.isoformat())
ui.done_loading(data_loader)
with head_r:
    st.markdown(
        f'<div class="sync-note">Jira data synced {breach_logic.to_jira_time(last_sync):%Y-%m-%d %H:%M %Z} · '
        f'{len(tickets):,} tickets since {tickets["created_date"].min():%d %b %Y}'
        f'<br>All dates and times in {html.escape(breach_logic.jira_time_label())}</div>',
        unsafe_allow_html=True,
    )

today = ta.jira_today(tickets)  # Jira timezone, not the PC clock
period = ui.period_picker(today, key="trend")
start, end, period_label = period["start"], period["end"], period["label"]
prev_range, prev_label = period["prev_range"], period["prev_label"]
with period["extra_col"]:
    trend_weeks = st.slider("Weekly trend: weeks shown", 4, config.TREND_HISTORY_DAYS // 7, 12)

# The Top 5 / 10 / 20 buttons sit in the "Top N drivers" title bar further
# down, but the overview and alerts above it already depend on the choice,
# so it is read from the widget's state here (it holds the last click).
# Clicking the selected button again clears it; keep the last choice instead
# (set before the widget is created, which Streamlit allows).
DEFAULT_TOP = f"Top {config.TREND_TOP_N}"
if not st.session_state.get("trend_top_n"):
    st.session_state["trend_top_n"] = st.session_state.get("trend_top_n_last", DEFAULT_TOP)
st.session_state["trend_top_n_last"] = st.session_state["trend_top_n"]
top_n = TOP_N_OPTIONS[st.session_state["trend_top_n"]]


# ---------------------------------------------------------------------------
# Analysis for the selected period
# ---------------------------------------------------------------------------
@st.cache_data(show_spinner=False)
def analyse(sync_stamp, start, end, prev_range, top_n):
    results = {}
    for cat in ta.CATEGORIES:
        top = ta.top_drivers(tickets, cat, start, end, prev_range, n=top_n)
        for d in top["drivers"]:
            summary = ta.driver_summary(tickets, cat, d["driver"], start, end)
            d["summary"] = summary["text"]
            d["tickets"] = summary["tickets"]
            d["samples"] = summary["tickets"].head(SAMPLE_TICKETS)[["key", "summary"]].to_dict("records")
            d["first_seen"] = summary["all_tickets"]["created_date"].min()
            d["source"] = ta.driver_source_of(summary["all_tickets"])
            d["fixes"] = ta.match_fixes(tickets, fixes, cat, d["driver"], end, index=fix_idx)
        results[cat] = top
    return results


# Stays up until the whole page below is drawn (cleared at the end of the script).
page_loader = ui.loading_if_new(
    "trend_loaded_view", (last_sync.isoformat(), start, end, prev_range, top_n),
    f"Analysing the top {top_n} drivers&hellip; matching SAL / SFS fixes and building the charts")
results = analyse(last_sync.isoformat(), start, end, prev_range, top_n)


delta_html = ui.delta_html  # more tickets = worse (red)


st.markdown(f'<div class="section-title">Overview &mdash; {html.escape(period_label)}</div>', unsafe_allow_html=True)
kpi_cols = st.columns(len(ta.CATEGORIES))
def issue_alerts(cat, d):
    """One alert per issue and kind: the most recent fix carries it, other
    fixes with the same alert (e.g. the SFS beside a SAL) are named in it."""
    out = []
    for kind in ("recurring", "overdue"):
        hits = sorted((f for f in d["fixes"] if f["alert"] == kind),
                      key=lambda f: f["date"] or date.min, reverse=True)
        if hits:
            out.append((cat, d, hits[0], hits[1:]))
    return out


all_alerts = []
for col, cat in zip(kpi_cols, ta.CATEGORIES):
    r = results[cat]
    alerts = [a for d in r["drivers"] for a in issue_alerts(cat, d)]
    all_alerts.extend(alerts)
    recurring = sum(1 for _, _, f, _ in alerts if f["alert"] == "recurring")
    col.markdown(
        f'<div class="kpi" style="--cat:{CATEGORY_COLOR[cat]};"><div class="kpi-label">{cat} tickets</div>'
        f'<div class="kpi-value">{r["total"]:,}</div>'
        f'<div class="kpi-sub">{delta_html(r["total"], r["prev_total"])} vs {prev_label} ({r["prev_total"]:,})'
        f' · top {len(r["drivers"])} drivers = {sum(d["count"] for d in r["drivers"]):,}'
        f' · {recurring} issue(s) still occurring after fix</div></div>',
        unsafe_allow_html=True,
    )

if all_alerts:
    st.markdown('<div class="section-title">Fix alerts on the top drivers</div>', unsafe_allow_html=True)
    order = {"recurring": 0, "overdue": 1}
    for cat, d, f, others in sorted(all_alerts, key=lambda a: (order[a[2]["alert"]], -a[2]["since_count"])):
        tag = "Still occurring after fix" if f["alert"] == "recurring" else "Fix date passed"
        also = ""
        if others:
            verb = {"deployed": "deployed", "resolved": "resolved"}
            also = " · also " + ", ".join(
                f'<a class="fix-key" href="{jira_url(o["key"])}" target="_blank">{o["key"]}</a>'
                + (f' ({verb.get(o["state"], "planned")} {o["date"]:%d %b %Y})' if o["date"] else "")
                for o in others)
        st.markdown(
            f'<div class="alert-box alert-{f["alert"]}"><span class="alert-tag">{tag}</span>'
            f'<span><b>{cat}</b> · {html.escape(d["driver"])} &rarr; '
            f'<a class="fix-key" href="{jira_url(f["key"])}" target="_blank">{f["key"]}</a>: '
            f'{html.escape(f["alert_text"])}{also}</span></div>',
            unsafe_allow_html=True,
        )


# ---------------------------------------------------------------------------
# Charts
# ---------------------------------------------------------------------------
def short(text, n=60):
    return text if len(text) <= n else text[: n - 1] + "…"


DRIVER_LABEL_CHARS = 62  # wrap width of the driver names beside the bars


def top_bar_chart(drivers, color):
    """Horizontal bars, one per driver, with the full driver name wrapped over
    as many lines as it needs (each row is tall enough for the longest)."""
    wrapped = ["\n".join(textwrap.wrap(d["driver"], DRIVER_LABEL_CHARS, break_long_words=True)) or d["driver"]
               for d in drivers]
    data = pd.DataFrame([{"Driver": w, "Full driver": d["driver"], "Tickets": d["count"],
                          "Share": f'{d["share"]:.0f}%', "Previous period": d["prev"]}
                         for w, d in zip(wrapped, drivers)])
    lines = max(w.count("\n") + 1 for w in wrapped)
    row = max(36, 15 * lines + 14)
    x_max = max(d["count"] for d in drivers) * 1.12 + 0.5  # room for the count beside the longest bar
    base = alt.Chart(data).encode(
        y=alt.Y("Driver:N", sort="-x", title=None,
                axis=alt.Axis(labelLimit=0, labelColor=TEXT_SECONDARY, labelExpr="split(datum.label, '\\n')",
                              labelLineHeight=15, labelPadding=8)),
        x=alt.X("Tickets:Q", title=None, scale=alt.Scale(domain=[0, x_max], nice=False),
                axis=alt.Axis(grid=True, gridColor="rgba(255,255,255,0.06)", labelColor=TEXT_MUTED, tickMinStep=1)),
        tooltip=["Full driver", "Tickets", "Share", "Previous period"],
    )
    bars = base.mark_bar(color=color, cornerRadiusEnd=4, height=18)
    labels = base.mark_text(align="left", dx=6, color=TEXT_PRIMARY, fontWeight="bold").encode(text="Tickets:Q")
    return (bars + labels).properties(height=max(120, row * len(drivers))).configure_view(stroke=None)\
        .configure(background="transparent")


def weekly_chart(trend, top):
    """Top drivers only: "Other" usually dwarfs them, so it stays in the table."""
    colors = SERIES_COLORS[: len(top)]
    trend = trend[trend["driver"] != "Other"]
    trend = trend.assign(Driver=trend["driver"].map(lambda d: short(d, 45)),
                         Week=trend["week"].map(lambda w: f"{w:%d %b}"))
    weeks = [f"{w:%d %b}" for w in sorted(trend["week"].unique())]
    domain = [short(d, 45) for d in top]
    hover = alt.selection_point(fields=["Week"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(trend).encode(
        x=alt.X("Week:O", sort=weeks, title="Week starting (Sunday)",
                axis=alt.Axis(labelAngle=0, labelColor=TEXT_MUTED, titleColor=TEXT_MUTED, grid=False)),
        y=alt.Y("count:Q", title="Tickets", axis=alt.Axis(gridColor="rgba(255,255,255,0.06)",
                                                          labelColor=TEXT_MUTED, titleColor=TEXT_MUTED)),
        color=alt.Color("Driver:N", scale=alt.Scale(domain=domain, range=colors),
                        legend=alt.Legend(orient="bottom", columns=2, labelLimit=420, title=None,
                                          labelColor=TEXT_SECONDARY, symbolStrokeWidth=3)),
    )
    lines = base.mark_line(strokeWidth=2, interpolate="monotone")
    points = base.mark_point(filled=True, size=70, stroke=SURFACE_CARD, strokeWidth=2).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("week_label:N", title="Week"), alt.Tooltip("driver:N", title="Driver"),
                 alt.Tooltip("count:Q", title="Tickets")],
    ).add_params(hover)
    rule = alt.Chart(trend).mark_rule(color="rgba(255,255,255,0.25)").encode(
        x=alt.X("Week:O", sort=weeks)).transform_filter(hover)
    return (lines + points + rule).properties(height=320).configure_view(stroke=None)\
        .configure(background="transparent")


def sample_tickets_html(samples):
    links = "".join(
        f'<a class="sample-key" href="{jira_url(s["key"])}" target="_blank" rel="noopener" '
        f'title="{html.escape(s["summary"], quote=True)}">{html.escape(s["key"])}</a>'
        for s in samples
    )
    return f'<div class="sample-row"><span>Sample tickets (latest {len(samples)}):</span>{links}</div>'


def fix_rows_html(fix_list):
    out = []
    for project in ("SAL", "SFS"):
        items = [f for f in fix_list if f["project"] == project]
        if not items:
            out.append(f'<div class="fix-row"><span class="fix-proj">{project}</span>'
                       f'<span class="fix-none">No {project} ticket in place</span></div>')
            continue
        for f in items:
            date_txt = ""
            if f["date"]:
                verb = {"deployed": "", "resolved": " · Resolved"}.get(f["state"], " · Planned")
                date_txt = f'{verb} {f["date"]:%d %b %Y}'
            elif f["state"] in ("in_progress", "not_started"):
                date_txt = " · No deployment date planned"
            match = (f'Linked to {f["linked_tickets"]} ticket(s)' if f["linked_tickets"]
                     else f'Linked via {f["via"]}' if f["via"] else f["match"])
            alert = (f'<div class="fix-alert {f["alert"]}">⚠ {html.escape(f["alert_text"])}</div>'
                     if f["alert"] else "")
            out.append(
                f'<div class="fix-row"><span class="fix-proj">{project}</span>'
                f'<a class="fix-key" href="{jira_url(f["key"])}" target="_blank" rel="noopener">{f["key"]}</a>'
                f'<span class="fix-chip" style="background:{FIX_STATE_COLOR[f["state"]]};">'
                f'{html.escape(f["label"])}{date_txt}</span>'
                f'<span class="fix-match">{match}</span>'
                f'<span class="fix-summary" title="{html.escape(f["summary"], quote=True)}">{html.escape(f["summary"])}</span>'
                f'{alert}</div>'
            )
    return "".join(out)


# ---------------------------------------------------------------------------
# Per-category tabs
# ---------------------------------------------------------------------------
title_col, top_col = st.columns([5, 1.25], vertical_alignment="center")
title_col.markdown(f'<div class="section-title">Top {top_n} drivers &mdash; {html.escape(period_label)}</div>',
                   unsafe_allow_html=True)
with top_col:
    st.segmented_control("Show", list(TOP_N_OPTIONS), key="trend_top_n",  # value set above
                         label_visibility="collapsed", width="stretch")
weekly = {}
tabs = st.tabs([f"{cat}  ({results[cat]['total']:,})" for cat in ta.CATEGORIES])
for tab, cat in zip(tabs, ta.CATEGORIES):
    r = results[cat]
    with tab:
        if not r["drivers"]:
            st.info(f"No {cat} tickets created in {period_label}.")
        else:
            if r["uncategorised"]:
                st.caption(f"{r['uncategorised']} ticket(s) with no category and no recognisable summary are "
                           "counted in the total but not ranked.")
            st.altair_chart(top_bar_chart(r["drivers"], CATEGORY_COLOR[cat]), width="stretch")
            for d in r["drivers"]:
                first_seen = f'{d["first_seen"]:%d %b %Y}' if pd.notna(d["first_seen"]) else "—"
                st.markdown(
                    f'<div class="driver-card"><div class="driver-head">'
                    f'<span class="driver-rank">#{d["rank"]}</span>'
                    f'<span class="driver-name">{html.escape(d["driver"])}</span>'
                    f'<span class="driver-count">{d["count"]:,}</span></div>'
                    f'<div class="driver-meta">{d["share"]:.0f}% of {cat} tickets · {delta_html(d["count"], d["prev"])} '
                    f'vs {prev_label} ({d["prev"]}) · grouped by {html.escape(d["source"])} · first seen {first_seen}</div>'
                    f'<div class="driver-summary">{html.escape(d["summary"])}</div>'
                    f'{sample_tickets_html(d["samples"])}'
                    f'{fix_rows_html(d["fixes"])}</div>',
                    unsafe_allow_html=True,
                )
                with st.expander(f'Tickets for #{d["rank"]} ({d["count"]})'):
                    t = d["tickets"]
                    breakdown = {"SAT": "Channel", "Order Fallout": "Account Type"}.get(cat, "Reporting Area")
                    cols = {"key": "Key", "created": "Created", "status": "Status", "sub_team": breakdown,
                            "severity": "Severity", "summary": "Summary"}
                    if cat == "SAT":
                        cols = {"key": "Key", "created": "Created", "status": "Status", "sub_team": "Channel",
                                "summary": "Summary", "sat_issue": "Issue Identified", "sat_subcat": "Issue Sub-Category",
                                "order_type": "Type of Order", "customer_type": "Customer Type"}
                    elif d["source"] == ta.FALLOUT_SOURCE:
                        cols.update({"of_reason": "Fallout Reason", "of_step": "Failed Step", "of_error": "Error"})
                    else:
                        cols.update({"inc_cat": "Incident Category", "inc_subcat": "Incident Sub Category"})
                    view = t[list(cols)].rename(columns=cols)
                    view["Created"] = view["Created"].str[:16].str.replace("T", " ")
                    view["Linked"] = t["links"].map(", ".join).values
                    view.insert(0, "S.No", range(1, len(view) + 1))
                    st.markdown(ui_table.html_table(view, max_height=400), unsafe_allow_html=True)

        if r["drivers"]:
            # build_excel is defined further down; the lambda runs it on click.
            st.download_button(
                f"⬇️ Download {cat} top {len(r['drivers'])} drivers with ticket details (Excel)",
                data=lambda c=cat: build_excel((c,)),
                file_name=f"Top_Drivers_{cat.replace(' ', '_')}_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}_Top{top_n}.xlsx",
                mime=EXCEL_MIME, on_click="ignore", key=f"download_{cat}",
                help=f"{cat}: the top drivers with summaries and SAL / SFS status, plus every ticket "
                     "behind them (Jira key, created, status, details) with clickable Jira links.",
            )

        st.markdown(f'<div class="section-title">{cat} &mdash; weekly trend (Sunday – Saturday), '
                    f'last {trend_weeks} weeks</div>', unsafe_allow_html=True)
        trend, top = ta.weekly_trend(tickets, cat, trend_weeks, end)
        weekly[cat] = trend
        if trend.empty:
            st.info("No tickets in this window.")
        else:
            st.altair_chart(weekly_chart(trend, top), width="stretch")
            st.caption(f"Chart shows the top {len(top)} drivers of the window (more lines can't be told apart); "
                       "all other drivers are in the table "
                       f"(\"Other\" row). The week of {ta.week_label(ta.week_start(end))} runs to "
                       f"{end:%d %b}" + (" (in progress)." if end == today else "."))
            with st.expander("Weekly trend as a table"):
                pivot = trend.pivot_table(index="driver", columns="week_label", values="count", sort=False)
                pivot = pivot[[ta.week_label(w) for w in sorted(trend["week"].unique())]]
                pivot.index.name = "Driver"
                st.markdown(ui_table.html_table(pivot, index=True, kinds={c: "heat" for c in pivot.columns},
                                                heat_by="row", max_height=420,
                                                caption="Shading compares each driver with its own busiest week."),
                            unsafe_allow_html=True)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def build_excel(categories=tuple(ta.CATEGORIES)):
    """Workbook for the given categories: top drivers, SAL / SFS fix tracking,
    weekly trend and every ticket behind the top drivers (keys link to Jira).
    Built only when a download button is clicked."""
    drivers_rows, ticket_frames, trend_frames = [], [], []
    for cat in categories:
        for d in results[cat]["drivers"]:
            alerts = "; ".join(f"{f['key']}: {f['alert_text']}" for f in d["fixes"] if f["alert"])
            drivers_rows.append({
                "Period": period_label, "Timezone": breach_logic.jira_time_label(), "Category": cat, "Rank": d["rank"], "Driver": d["driver"],
                "Tickets": d["count"], "Share %": d["share"], "Compared with": prev_label,
                "Tickets then": d["prev"],
                "Grouped by": d["source"], "Summary": d["summary"],
                "Sample tickets": ", ".join(s["key"] for s in d["samples"]),
                "SAL in place": ta.fix_status_text(d["fixes"], "SAL"),
                "SFS in place": ta.fix_status_text(d["fixes"], "SFS"),
                "Alert": alerts,
            })
            t = d["tickets"].assign(Category=cat, Driver=d["driver"], Rank=d["rank"])
            ticket_frames.append(t)
        if cat in weekly and not weekly[cat].empty:
            trend_frames.append(weekly[cat].assign(Category=cat))

    fix_rows = []
    for cat in categories:
        for d in results[cat]["drivers"]:
            for f in d["fixes"]:
                fix_rows.append({"Category": cat, "Rank": d["rank"], "Driver": d["driver"], "Ticket": f["key"],
                                 "Project": f["project"], "Match": f["match"], "Jira status": f["status"],
                                 "Fix state": f["label"], "Date": f["date"].isoformat() if f["date"] else "",
                                 "Alert": f["alert_text"], "Summary": f["summary"], "Link": jira_url(f["key"])})

    sheets = {"Top Drivers": pd.DataFrame(drivers_rows), "Fix Tracking (SAL-SFS)": pd.DataFrame(fix_rows)}
    if trend_frames:
        tr = pd.concat(trend_frames)
        sheets["Weekly Trend"] = tr.pivot_table(index=["Category", "driver"], columns="week", values="count",
                                                sort=False).rename(columns=ta.week_label).reset_index()\
            .rename(columns={"driver": "Driver"})
    if ticket_frames:
        tk = pd.concat(ticket_frames)
        tickets_sheet = pd.DataFrame({
            "Category": tk["Category"], "Rank": tk["Rank"], "Driver": tk["Driver"], "Key": tk["key"],
            "Created": tk["created"].str[:16].str.replace("T", " "), "Status": tk["status"],
            "Reporting Area / Account / Channel": tk["sub_team"], "Severity": tk["severity"], "Summary": tk["summary"],
            "Incident Sub Category": tk["inc_subcat"], "Fallout Reason": tk["of_reason"],
            "Failed Step": tk["of_step"], "Error": tk["of_error"],
            "SAT Issue Identified": tk["sat_issue"], "SAT Issue Sub-Category": tk["sat_subcat"],
            "SAT Type of Order": tk["order_type"], "SAT Customer Type": tk["customer_type"],
            "Linked": tk["links"].map(", ".join), "Link": tk["key"].map(jira_url),
        })
        # Drop columns no ticket here uses (e.g. SAT fields in a SAC download).
        blank = [c for c in tickets_sheet.columns if tickets_sheet[c].fillna("").astype(str).str.strip().eq("").all()]
        sheets["Tickets"] = tickets_sheet.drop(columns=blank)

    return ui.write_excel(sheets)



st.markdown('<div class="section-title">Download</div>', unsafe_allow_html=True)
st.download_button(
    "⬇️ Download trend analysis — all categories (Excel)",
    data=build_excel,
    file_name=f"Ticket_Trend_Analysis_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}_Top{top_n}.xlsx",
    mime=EXCEL_MIME, on_click="ignore",
    help="Top drivers with summaries, SAL / SFS fix tracking and alerts, weekly trend and every ticket "
         "behind the top drivers, with clickable Jira links.",
)

ui.done_loading(page_loader)
