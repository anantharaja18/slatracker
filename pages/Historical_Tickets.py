"""
Resolved Tickets History: the dashboard teams' (Retail, SAC, B2B, Order
Fallout) resolved tickets for any day, Sunday-Saturday week or custom range
-- SLA per team, daily / weekly trend, resolved by assignee and the ticket
list, with an Excel download.

Opened in a new browser tab from the dashboard's "Resolved History" button.
Data comes from the local cache kept by trend_store.py.
"""

import html
from datetime import datetime, timedelta

import altair as alt
import pandas as pd
import streamlit as st
from streamlit_autorefresh import st_autorefresh

import breach_logic
import config
import resolved_analysis as ra
import trend_analysis as ta
import trend_store
import ui_common as ui
import popup_ui
import sla_comments_ui
import ui_table
from sla_views import (TEAM_COLOR, count_cell, pct_color, project_html, project_png,
                       project_summary_frame, sla_cell)
from ui_common import (ACCENT_FROM, ACCENT_TO, COLOR_CRITICAL, COLOR_GOOD, COLOR_WARNING, HEADER_BG, SURFACE_CARD,
                       SURFACE_PANEL, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY, jira_url)

ALL_COLOR = "#9085e9"

st.set_page_config(page_title="Historical Tickets", page_icon="✅", layout="wide")
if not sla_comments_ui.comments_open("hist"):  # paused while the comments dialog is open
    st_autorefresh(interval=5 * 60 * 1000, key="resolved_autorefresh")
ui.apply_css(f"""
    .kpi-row {{ display: flex; gap: 14px; align-items: baseline; flex-wrap: wrap; }}
    .kpi-pct {{ font-size: 1.15rem; font-weight: 700; }}
    .kpi-split {{ font-size: 0.72rem; color: {TEXT_MUTED}; margin-top: 2px; }}

    .asg-wrap {{ max-height: 560px; overflow: auto; border: 1px solid rgba(255,255,255,0.08);
                 border-radius: 12px; background: {SURFACE_PANEL}; }}
    .asg-table {{ width: 100%; border-collapse: separate; border-spacing: 0; font-size: 0.84rem;
                  color: {TEXT_SECONDARY}; font-variant-numeric: tabular-nums; }}
    .asg-table th {{
        position: sticky; top: 0; z-index: 2; background: {HEADER_BG}; color: {TEXT_MUTED};
        font-size: 0.66rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
        text-align: right; padding: 10px 14px; white-space: nowrap; border-bottom: 1px solid rgba(255,255,255,0.1);
    }}
    .asg-table th:first-child, .asg-table td:first-child {{ text-align: left; }}
    .asg-table th .dot {{ width: 7px; height: 7px; border-radius: 50%; display: inline-block; margin-right: 6px; }}
    .asg-table td {{ text-align: right; padding: 8px 14px; border-top: 1px solid rgba(255,255,255,0.05); white-space: nowrap; }}
    .asg-table tbody tr:hover td {{ background: rgba(255,255,255,0.03); }}
    .asg-name {{ display: flex; align-items: center; gap: 10px; color: {TEXT_PRIMARY}; font-weight: 600; }}
    .asg-avatar {{
        width: 28px; height: 28px; border-radius: 50%; flex-shrink: 0; display: inline-flex;
        align-items: center; justify-content: center; font-size: 0.68rem; font-weight: 700; color: white;
        background: linear-gradient(135deg, {ACCENT_FROM}, {ACCENT_TO});
    }}
    .asg-zero {{ color: rgba(255,255,255,0.22); }}
    .asg-total {{ display: flex; align-items: center; justify-content: flex-end; gap: 10px;
                  color: {TEXT_PRIMARY}; font-weight: 700; }}
    .asg-bar {{ width: 90px; height: 6px; border-radius: 999px; background: rgba(255,255,255,0.07); overflow: hidden; }}
    .asg-bar span {{ display: block; height: 100%; border-radius: 999px;
                     background: linear-gradient(90deg, {ACCENT_FROM}, {ACCENT_TO}); }}
    .asg-good {{ color: {COLOR_GOOD}; font-weight: 600; }}
    .asg-bad {{ color: {COLOR_CRITICAL}; font-weight: 600; }}
    .asg-sla {{ display: inline-flex; align-items: center; gap: 8px; }}
    .asg-pill {{
        min-width: 58px; text-align: center; padding: 2px 10px; border-radius: 999px;
        font-size: 0.76rem; font-weight: 700; border: 1px solid;
    }}
    .asg-slabar {{ width: 56px; height: 5px; border-radius: 999px; background: rgba(255,255,255,0.07); overflow: hidden; }}
    .asg-slabar span {{ display: block; height: 100%; border-radius: 999px; }}
    .asg-table tr.asg-grand td {{
        position: sticky; bottom: 0; z-index: 1; background: {HEADER_BG}; color: {TEXT_PRIMARY}; font-weight: 700;
        border-top: 1px solid rgba(255,255,255,0.14);
    }}
    .asg-legend {{ font-size: 0.7rem; color: {TEXT_MUTED}; margin: 6px 2px 0 2px; }}
    .asg-legend b {{ font-weight: 700; }}
""")


def initials(name):
    parts = [p for p in str(name).replace(".", " ").split() if p[:1].isalpha()]
    return ((parts[0][0] + (parts[-1][0] if len(parts) > 1 else "")) if parts else "?").upper()


def assignee_table_html(table, teams):
    """The Resolved-by-assignee table (ra.by_assignee output, Grand Total last)."""
    rows, total = table.iloc[:-1], table.iloc[-1]
    busiest = max(int(rows["Total Resolved"].max() or 0), 1)

    def count(v, cls=""):
        v = int(v)
        return '<span class="asg-zero">—</span>' if v == 0 else f'<span class="{cls}">{v:,}</span>'

    team_cols = [t for t in teams if f"{t} (Resolved)" in table.columns]
    head = ('<th>Assignee</th>'
            + "".join(f'<th><span class="dot" style="background:{TEAM_COLOR[t]};"></span>{html.escape(t)}</th>'
                      for t in team_cols)
            + '<th>Total resolved</th>'
            + '<th title="Created on the same day it was resolved">Resolved from<br>today\'s inflow</th>'
            + '<th title="Created on an earlier day than it was resolved">Resolved from<br>past inflow</th>'
            + '<th>SLA Met</th><th>SLA not Met</th><th>SLA %</th>')
    body = []
    for _, r in rows.iterrows():
        width = int(r["Total Resolved"]) / busiest * 100
        body.append(
            f'<tr><td><span class="asg-name"><span class="asg-avatar">{html.escape(initials(r["Assignee"]))}</span>'
            f'{html.escape(str(r["Assignee"]))}</span></td>'
            + "".join(f'<td>{count(r[f"{t} (Resolved)"])}</td>' for t in team_cols)
            + f'<td><span class="asg-total"><span class="asg-bar"><span style="width:{width:.0f}%;"></span></span>'
              f'{int(r["Total Resolved"]):,}</span></td>'
            + f'<td>{count(r[ra.INFLOW_SAME_DAY])}</td><td>{count(r[ra.INFLOW_PAST])}</td>'
            + f'<td>{count(r["SLA Met"], "asg-good")}</td><td>{count(r["SLA not Met"], "asg-bad")}</td>'
            + f'<td>{sla_cell(r["SLA %"])}</td></tr>'
        )
    grand = (f'<tr class="asg-grand"><td>Grand Total ({len(rows)} assignee{"s" if len(rows) != 1 else ""})</td>'
             + "".join(f'<td>{int(total[f"{t} (Resolved)"]):,}</td>' for t in team_cols)
             + f'<td>{int(total["Total Resolved"]):,}</td>'
               f'<td>{int(total[ra.INFLOW_SAME_DAY]):,}</td><td>{int(total[ra.INFLOW_PAST]):,}</td><td class="asg-good">{int(total["SLA Met"]):,}</td>'
               f'<td class="asg-bad">{int(total["SLA not Met"]):,}</td><td>{sla_cell(total["SLA %"])}</td></tr>')
    return (f'<div class="asg-wrap"><table class="asg-table"><thead><tr>{head}</tr></thead>'
            f'<tbody>{"".join(body)}{grand}</tbody></table></div>'
            f'<div class="asg-legend">SLA %: <b style="color:{COLOR_GOOD};">90% and above</b> · '
            f'<b style="color:{COLOR_WARNING};">70 – 89%</b> · <b style="color:{COLOR_CRITICAL};">below 70%</b> '
            '· SLA % = SLA Met ÷ (SLA Met + SLA not Met)</div>')


def inflow_sheets(start, end, resolved):
    """Excel sheets for the ticket details, split by when each ticket was created:
    resolved from today's inflow (created the same day) and from past inflow."""
    def sheet(which):
        part = ra.ticket_list(ra.by_inflow(resolved, which), start, end)
        return part.assign(Link=part["Key"].map(jira_url))

    return {"Resolved from Today's Inflow": sheet(ra.INFLOW_SAME_DAY),
            "Resolved from Past Inflow": sheet(ra.INFLOW_PAST)}


def fmt_pct(pct):
    return "—" if pct is None else f"{pct:.0f}%"


def fmt_hours(h):
    if h is None or pd.isna(h):
        return "—"
    return f"{h * 60:.0f} min" if h < 1 else f"{h:.1f} h" if h < 48 else f"{h / 24:.1f} days"


# One shared, read-only copy per server, latest sync only: st.cache_data would
# pickle ~100k rows and hand each visitor a copy (it ran out of memory).
@st.cache_resource(show_spinner=False, max_entries=1)
def load_data(sync_stamp):
    return ra.load_resolved()


@st.cache_data(ttl=300, show_spinner=False)  # live from Jira: open tickets breached right now
def load_open_breached():
    return ra.load_open_breached()


head_r = ui.page_header(
    "Historical Tickets", "Resolved tickets and SLA for Retail, SAC, B2B and Order Fallout — any day, week or range",
    '<path d="M20 6L9 17l-5-5"/>')
with head_r:
    force = st.button("🔄  Sync with Jira now", width="stretch")

# Show the loading line from the very start of a page's first load (the sync
# check below can take a while); later reruns, such as auto-refresh, stay quiet.
first_load = not st.session_state.get("resolved_page_loaded")
boot = ui.loading("Loading resolved tickets&hellip; checking the Jira sync") if first_load else st.empty()
ui.ensure_fresh(force=force)
boot.empty()
st.session_state["resolved_page_loaded"] = True
last_sync = trend_store.last_sync_time()
data_loader = ui.loading_if_new("resolved_loaded_sync", last_sync.isoformat() if last_sync else None,
                                "Loading resolved tickets&hellip; a year of Retail, SAC, B2B and Order Fallout")
resolved = load_data(last_sync.isoformat()) if last_sync else pd.DataFrame()
ui.done_loading(data_loader)
if resolved.empty:
    st.error("No resolved-ticket data yet: the Jira sync has not completed. Check the Jira credentials in .env and retry.")
    st.stop()
with head_r:
    st.markdown(
        f'<div class="sync-note">Jira data synced {breach_logic.to_jira_time(last_sync):%Y-%m-%d %H:%M %Z} · {len(resolved):,} resolved '
        f'tickets since {resolved["resolved_date"].min():%d %b %Y}'
        f'<br>All dates and times in {html.escape(breach_logic.jira_time_label())}</div>',
        unsafe_allow_html=True,
    )

today = ta.jira_today(resolved)  # Jira timezone, not the PC clock
period = ui.period_picker(today, key="resolved")
start, end, label = period["start"], period["end"], period["label"]
prev_label = period["prev_label"]

# Trend window: trailing days for a day, trailing weeks for a week, the range
# itself for a custom range (by day up to ~2 months, by week beyond that).
with period["extra_col"]:
    if period["mode"] == ui.DAY:
        n = st.slider("Daily trend: days shown", 7, 90, 14, key="resolved_days")
        trend_by, trend_first, trend_last = "day", end - timedelta(days=n - 1), end
    elif period["mode"] == ui.WEEK:
        n = st.slider("Weekly trend: weeks shown", 4, config.TREND_HISTORY_DAYS // 7, 12, key="resolved_weeks")
        trend_by, trend_first, trend_last = "week", start - timedelta(weeks=n - 1), end
    else:
        default = 0 if (end - start).days <= 62 else 1
        trend_by = "day" if st.radio("Trend by", ["Day", "Week (Sun – Sat)"], index=default,
                                     horizontal=True, key="resolved_by") == "Day" else "week"
        trend_first, trend_last = start, end

# ---------------------------------------------------------------------------
# Summary tiles
# ---------------------------------------------------------------------------
# Open tickets breached right now come live from Jira (cached 5 minutes): show a loading line while that runs.
now = datetime.now()
open_loader = ui.loading_if_new("historical_open_breached", f"{now:%Y%m%d%H}{now.minute // 5}",
                                "Loading open tickets from Jira&hellip; checking which are breached now")
open_rows = load_open_breached()
ui.done_loading(open_loader)
summary = ra.created_summary(resolved, open_rows, start, end, period["prev_range"])
st.markdown(f'<div class="section-title">Tickets created &mdash; {html.escape(label)}</div>', unsafe_allow_html=True)
cols = st.columns(len(ra.TEAMS) + 1)
for col, team in zip(cols, ["All teams", *ra.TEAMS]):
    s = summary[team]
    color = ALL_COLOR if team == "All teams" else TEAM_COLOR[team]
    col.markdown(
        f'<div class="kpi" style="--cat:{color};"><div class="kpi-label">{team}</div>'
        f'<div class="kpi-row"><span class="kpi-value">{s["resolved"]:,}</span>'
        f'<span class="kpi-pct" style="color:{pct_color(s["pct"])};" title="SLA Met / (SLA Met + SLA not Met)">'
        f'{fmt_pct(s["pct"])} SLA</span></div>'
        f'<div class="kpi-sub">{ui.delta_html(s["resolved"], s["prev"]["resolved"], higher_is_good=True)} vs '
        f'{prev_label} ({s["prev"]["resolved"]:,}, SLA {fmt_pct(s["prev"]["pct"])})</div>'
        f'<div class="kpi-split">{s["within"]:,} SLA Met · {s["breached"]:,} SLA not Met '
        f'({s["late"]:,} SLA not Met on resolved + {s["open_breached"]:,} SLA not Met still open)</div></div>',
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# SLA by severity
# ---------------------------------------------------------------------------
with st.container(border=True):
    sev_head_title, sev_head_xlsx, sev_head_png = st.columns([3, 1.4, 1.4], vertical_alignment="center")
    with sev_head_title:
        st.markdown(f'<div style="font-size:0.95rem;font-weight:700;color:#F5F5F7;padding-top:6px;">SLA by severity, tickets created &mdash; {html.escape(label)}</div>', unsafe_allow_html=True)
    st.markdown(f"<style>{popup_ui.POPUP_CSS}</style>", unsafe_allow_html=True)
    st.html(popup_ui.POPUP_SCRIPT, unsafe_allow_javascript=True)

    def breached_popup(team, sev, count_html, count):
        """Hover popup on a SLA not Met count: the tickets behind it, with key, severity,
        assignee and status."""
        tickets = ra.breached_tickets(resolved, open_rows, start, end, team, sev)
        return popup_ui.popup_row(count_html, popup_ui.bucket_popup_html(tickets),
                                  f"{team} — {sev} SLA not Met ({count})", row_class="asg-popup-cell")

    st.markdown(project_html(ra.sla_by_created(resolved, open_rows, start, end), wrap_breached=breached_popup),
                unsafe_allow_html=True)
    sla_text = ", ".join(f"{sev} {mins // 60} h" for sev, mins in config.SLA_MINUTES.items())
    st.caption(f"Tickets created in the period. SLA Met = resolved before Created + the severity's SLA ({sla_text}). "
               "SLA not Met = missed the SLA: resolved after it, or still open past it. SLA Met % = met ÷ (met + not met); "
               "Overall SLA % covers Sev-1 and Sev-2 together. Open tickets not yet breached, and tickets "
               "with no Sev-1 / Sev-2, are not counted.")


    def build_severity_excel(start=start, end=end, label=label, resolved=resolved, open_rows=open_rows):
        """Excel: Summary (this table) and the resolved tickets sheets."""
        return ui.write_excel({
            "Summary": project_summary_frame(ra.sla_by_created(resolved, open_rows, start, end)),
            **inflow_sheets(start, end, resolved),
        })


    def build_severity_png(start=start, end=end, label=label, resolved=resolved, open_rows=open_rows):
        """PNG of the Summary table only."""
        return project_png(ra.sla_by_created(resolved, open_rows, start, end),
                           f"SLA by severity, tickets created — {label}",
                           f"All dates and times in {breach_logic.jira_time_label()}")


    with sev_head_xlsx:
        st.download_button(
            "⬇️ Download SLA by severity (Excel)", data=build_severity_excel, on_click="ignore",
            key="download_sev_xlsx", file_name=f"SLA_By_Severity_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            help="Summary sheet (this table) and Tickets sheet (every resolved ticket in the period, with Jira links).",
        )
    with sev_head_png:
        st.download_button(
            "🖼️ Download SLA summary (PNG)", data=build_severity_png, on_click="ignore",
            key="download_sev_png", file_name=f"SLA_Summary_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.png",
            mime="image/png", help="The Summary table as an image.",
        )

# ---------------------------------------------------------------------------
# SLA miss comments: every breached ticket in the period (resolved late + open breached)
# ---------------------------------------------------------------------------
comment_tickets = [t for sev in config.SLA_MINUTES
                   for t in ra.breached_tickets(resolved, open_rows, start, end, config.TOTAL_LABEL, sev)]
sla_comments_ui.open_comments_button(comment_tickets, "hist", f"💬 SLA miss comments ({len(comment_tickets)} breached)")

# ---------------------------------------------------------------------------
# By assignee
# ---------------------------------------------------------------------------
st.markdown(f'<div class="section-title">Resolved by assignee &mdash; {html.escape(label)}</div>', unsafe_allow_html=True)
a1, a2 = st.columns([1, 1.6])
with a1:
    team_filter = st.multiselect("Teams", ra.TEAMS, default=ra.TEAMS, key="resolved_assignee_teams") or ra.TEAMS
period_part = ra.in_period(resolved, start, end)
period_part = period_part[period_part["category"].isin(team_filter)]
inflow_counts = {opt: len(ra.by_inflow(period_part, opt)) for opt in ra.INFLOW_OPTIONS}
inflow_filter = st.radio(
    "Resolved tickets", ra.INFLOW_OPTIONS, horizontal=True, key="resolved_assignee_inflow",
    format_func=lambda opt: f"{opt} ({inflow_counts[opt]:,})",
    help="The day's inflow = created on the same day it was resolved (the dashboard's \"Resolved from "
         "Today Inflow\"). Past inflow = created on an earlier day (\"Resolved from Past Inflow\").")
assignee_src = ra.by_inflow(resolved, inflow_filter)
with a2:
    people = ra.assignees_in(assignee_src, start, end, team_filter)
    person_filter = st.multiselect("Assignee", people, key="resolved_assignee_people",
                                   placeholder="All assignees — type a name to filter")
assignees = ra.by_assignee(assignee_src, start, end, team_filter, person_filter)
if assignees.empty:
    st.info("No resolved tickets for these teams and this selection in this period.")
else:
    st.markdown(assignee_table_html(assignees, team_filter), unsafe_allow_html=True)

    def build_assignee_excel(teams=tuple(team_filter), people=tuple(person_filter), table=assignees,
                             which=inflow_filter):
        """This panel as shown (teams / inflow / assignee filters, period) plus
        the resolved tickets behind it; built only when the button is clicked."""
        tickets_part = ra.ticket_list(ra.by_inflow(resolved, which), start, end)
        tickets_part = tickets_part[tickets_part["Team"].isin(teams)]
        if people:
            tickets_part = tickets_part[tickets_part["Assignee"].isin(people)]
        tickets_part = tickets_part.assign(**{"S.No": range(1, len(tickets_part) + 1)})
        info = pd.DataFrame([{"Period": label, "Timezone": breach_logic.jira_time_label(),
                              "Teams": ", ".join(teams), "Resolved tickets": which,
                              "Assignees": ", ".join(people) or "All"}])
        return ui.write_excel({
            "By Assignee": table,
            "Tickets": tickets_part.assign(Link=tickets_part["Key"].map(jira_url)),
            "Filters": info,
        })

    st.download_button(
        "⬇️ Download resolved by assignee (Excel)",
        data=build_assignee_excel, on_click="ignore", key="download_by_assignee",
        file_name=(f"Resolved_By_Assignee_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}"
                   f"{ {ra.INFLOW_SAME_DAY: '_day_inflow', ra.INFLOW_PAST: '_past_inflow'}.get(inflow_filter, '')}.xlsx"),
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        help="The table above (with your Teams / Assignee filters) and every resolved ticket behind it, "
             "with clickable Jira links.",
    )

# ---------------------------------------------------------------------------
# Trend
# ---------------------------------------------------------------------------
trend = ra.trend(resolved, trend_first, trend_last, trend_by)
unit = "week (Sunday – Saturday)" if trend_by == "week" else "day"
st.markdown(f'<div class="section-title">Trend per {unit} &mdash; {trend_first:%d %b %Y} to {trend_last:%d %b %Y}</div>',
            unsafe_allow_html=True)


def trend_chart(data, field, title, y_scale=None, teams=ra.TEAMS, height=280, legend=True):
    x_fmt = "%d %b"
    data = data[data["category"].isin(teams)]
    data = data.assign(Bucket=pd.to_datetime(data["bucket"]).dt.strftime(x_fmt))
    order = [pd.Timestamp(b).strftime(x_fmt) for b in sorted(data["bucket"].unique())]
    hover = alt.selection_point(fields=["Bucket"], nearest=True, on="pointerover", empty=False)
    base = alt.Chart(data).encode(
        x=alt.X("Bucket:O", sort=order, title=None,
                axis=alt.Axis(labelAngle=0, labelColor=TEXT_MUTED, labelOverlap=True, grid=False)),
        y=alt.Y(f"{field}:Q", title=title, scale=y_scale or alt.Undefined,
                axis=alt.Axis(gridColor="rgba(255,255,255,0.06)", labelColor=TEXT_MUTED, titleColor=TEXT_MUTED,
                              tickMinStep=1)),
        color=alt.Color("category:N", title=None, scale=alt.Scale(domain=teams, range=[TEAM_COLOR[t] for t in teams]),
                        legend=alt.Legend(orient="top", labelColor=TEXT_SECONDARY, symbolStrokeWidth=3) if legend else None),
    )
    lines = base.mark_line(strokeWidth=2, interpolate="monotone")
    points = base.mark_point(filled=True, size=70, stroke=SURFACE_CARD, strokeWidth=2).encode(
        opacity=alt.condition(hover, alt.value(1), alt.value(0)),
        tooltip=[alt.Tooltip("label:N", title="Day" if trend_by == "day" else "Week"),
                 alt.Tooltip("category:N", title="Team"), alt.Tooltip("resolved:Q", title="Resolved"),
                 alt.Tooltip("within:Q", title="SLA Met"), alt.Tooltip("late:Q", title="SLA not Met"),
                 alt.Tooltip("sla_pct:Q", title="SLA %", format=".1f")],
    ).add_params(hover)
    rule = alt.Chart(data).mark_rule(color="rgba(255,255,255,0.25)").encode(
        x=alt.X("Bucket:O", sort=order)).transform_filter(hover)
    return (lines + points + rule).properties(height=height).configure_view(stroke=None).configure(background="transparent")


# Resolved counts: one small chart per team, each on its own scale (Order
# Fallout resolves several times more than the others and would flatten them).
team_cols = st.columns(len(ra.TEAMS))
for col, team in zip(team_cols, ra.TEAMS):
    with col:
        st.markdown(f'<div class="kpi-label" style="color:{TEAM_COLOR[team]};">{team} &mdash; resolved</div>',
                    unsafe_allow_html=True)
        st.altair_chart(trend_chart(trend, "resolved", None, teams=[team], height=180, legend=False), width="stretch")
st.altair_chart(trend_chart(trend.dropna(subset=["sla_pct"]), "sla_pct", "SLA % (resolved)",
                            alt.Scale(domain=[0, 100])), width="stretch")
if trend_last == today:
    st.caption(f"The last {'week' if trend_by == 'week' else 'day'} shown is still in progress.")
with st.expander("Trend as a table"):
    pivot = trend.pivot_table(index="label", columns="category", values="resolved", sort=False)[ra.TEAMS]
    pct = trend.pivot_table(index="label", columns="category", values="sla_pct", sort=False).reindex(columns=ra.TEAMS)
    table = pivot.join(pct.add_suffix(" SLA %"))
    table.index.name = "Day" if trend_by == "day" else "Week"
    st.markdown(ui_table.html_table(table, index=True, kinds={t: "heat" for t in ra.TEAMS}, max_height=420),
                unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Ticket list
# ---------------------------------------------------------------------------
st.markdown(f'<div class="section-title">Resolved tickets &mdash; {html.escape(label)}</div>', unsafe_allow_html=True)
tickets = ra.ticket_list(resolved, start, end)
f1, f2, f4, f3 = st.columns([1.2, 1.3, 1.5, 1.4])
with f1:
    teams_shown = st.multiselect("Team", ra.TEAMS, default=ra.TEAMS, key="resolved_list_teams")
with f2:
    sla_shown = st.multiselect("SLA", [ra.WITHIN, ra.LATE, ra.NOT_JUDGED],
                               default=[ra.WITHIN, ra.LATE, ra.NOT_JUDGED], key="resolved_list_sla")
with f4:
    list_people = st.multiselect("Assignee", sorted(tickets["Assignee"].unique()), key="resolved_list_people",
                                 placeholder="All assignees")
with f3:
    search = st.text_input("Search key, summary or assignee", key="resolved_list_search").strip().lower()
shown = tickets[tickets["Team"].isin(teams_shown) & tickets["SLA"].isin(sla_shown)]
if list_people:
    shown = shown[shown["Assignee"].isin(list_people)]
if search:
    hay = (shown["Key"] + " " + shown["Summary"] + " " + shown["Assignee"]).str.lower()
    shown = shown[hay.str.contains(search, regex=False)]
shown = shown.assign(**{"S.No": range(1, len(shown) + 1)})
st.caption(f"{len(shown):,} of {len(tickets):,} resolved tickets")
st.markdown(ui_table.html_table(shown, max_height=520, kinds={"Created & Resolved Same Day": "text"}),
            unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------
def build_excel():
    summary_rows = [{
        "Period": label, "Timezone": breach_logic.jira_time_label(), "Team": team,
        "Tickets created (resolved)": s["resolved"], "SLA Met": s["within"],
        "SLA not Met (resolved)": s["late"], "SLA not Met (still open)": s["open_breached"], "SLA not Met": s["breached"],
        "SLA %": s["pct"],
        "Compared with": prev_label, "Resolved then": s["prev"]["resolved"], "SLA not Met then": s["prev"]["breached"],
        "SLA % then": s["prev"]["pct"],
    } for team, s in summary.items()]
    trend_sheet = trend.rename(columns={"label": "Day" if trend_by == "day" else "Week", "category": "Team",
                                        "resolved": "Resolved", "within": "SLA Met", "late": "SLA not Met",
                                        "sla_pct": "SLA %"}).drop(columns=["bucket"])
    return ui.write_excel({
        "Summary": pd.DataFrame(summary_rows),
        "Trend": trend_sheet,
        "SLA by Severity": project_summary_frame(ra.sla_by_created(resolved, open_rows, start, end)),
        "By Assignee": assignees if not assignees.empty else pd.DataFrame({"Assignee": []}),
        **inflow_sheets(start, end, resolved),
    })


st.markdown('<div class="section-title">Download</div>', unsafe_allow_html=True)
st.download_button(
    "⬇️ Download resolved history (Excel)",
    data=build_excel(),
    file_name=f"Resolved_Tickets_{start:%Y-%m-%d}_to_{end:%Y-%m-%d}.xlsx",
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    help="Per-team summary, trend, resolved by assignee and every resolved ticket in the period.",
)
