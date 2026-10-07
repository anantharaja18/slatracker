"""
Streamlit dashboard: live view of all 4 ticket categories with breach status.

Run with:
    streamlit run dashboard.py
"""

import html
import itertools
import io
from collections import defaultdict
from datetime import datetime

import pandas as pd
import streamlit as st
from openpyxl.styles import Font
from streamlit_autorefresh import st_autorefresh

# import ai_assistant  # AI chat assistant -- disabled until ANTHROPIC_API_KEY is set up
import config
import jira_client
import breach_logic


# ---------------------------------------------------------------------------
# Palette (validated categorical + fixed status colors, dark-surface safe)
# ---------------------------------------------------------------------------
SURFACE_PAGE = "#0d0d14"
SURFACE_CARD = "#14141c"
SURFACE_PANEL = "#1a1a24"
TEXT_PRIMARY = "#F5F5F7"
TEXT_SECONDARY = "#C3C2B7"
TEXT_MUTED = "#898781"
ACCENT_FROM = "#7C3AED"
ACCENT_TO = "#DB2777"
HEADER_BG = "#20202c"

COLOR_GOOD = "#0ca30c"
COLOR_CAUTION = "#7cb342"
COLOR_WARNING = "#fab219"
COLOR_SERIOUS = "#ec835a"
COLOR_CRITICAL = "#d03b3b"
COLOR_CRITICAL_DARK = "#7a1f1f"

STATUS_ORDER = [
    breach_logic.STATUS_OK,
    breach_logic.STATUS_WARNING,
    breach_logic.STATUS_BREACHED,
    breach_logic.STATUS_UNKNOWN,
]
STATUS_COLOR = {
    breach_logic.STATUS_OK: COLOR_GOOD,
    breach_logic.STATUS_WARNING: COLOR_WARNING,
    breach_logic.STATUS_BREACHED: COLOR_CRITICAL,
    breach_logic.STATUS_UNKNOWN: "#898781",
}

# Time-to-resolve buckets: every open ticket falls into exactly one, so a
# category's bucket counts always add up to its total open-ticket count.
BUCKET_BREACHED_PAST = "Breached (Past)"
BUCKET_BREACHED_TODAY = "Breached Today"
BUCKET_15 = "Within 15 mins"
BUCKET_30 = "Within 30 mins"
BUCKET_45 = "Within 45 mins"
BUCKET_OVER_1H = "> 1 hour"
BUCKET_ORDER = [BUCKET_BREACHED_PAST, BUCKET_BREACHED_TODAY, BUCKET_15, BUCKET_30, BUCKET_45, BUCKET_OVER_1H]
BUCKET_COLOR = {
    BUCKET_BREACHED_PAST: COLOR_CRITICAL_DARK,
    BUCKET_BREACHED_TODAY: COLOR_CRITICAL,
    BUCKET_15: COLOR_SERIOUS,
    BUCKET_30: COLOR_WARNING,
    BUCKET_45: COLOR_CAUTION,
    BUCKET_OVER_1H: COLOR_GOOD,
}

CATEGORY_COLOR = {
    "Retail": "#3987e5",
    "SAC": "#d95926",
    "B2B": "#199e70",
    "Order Fallout": "#c98500",
}
DEFAULT_CATEGORY_COLOR = "#3987e5"

# Jira workflow status -> dot color for the "Tickets by Status" cards. Falls
# back to a neutral gray for any status not explicitly mapped.
STATUS_DOT_COLOR = {
    "escalated": COLOR_CRITICAL,
    "in progress": COLOR_WARNING,
    "on hold": COLOR_WARNING,
    "pending": COLOR_WARNING,
    "waiting for customer": COLOR_WARNING,
    "waiting for support": COLOR_WARNING,
    "open": TEXT_MUTED,
    "to do": TEXT_MUTED,
    "new": TEXT_MUTED,
    "resolved": COLOR_GOOD,
    "done": COLOR_GOOD,
    "closed": COLOR_GOOD,
}
DEFAULT_STATUS_DOT_COLOR = "#5b5b56"


def status_dot_color(status):
    return STATUS_DOT_COLOR.get((status or "").strip().lower(), DEFAULT_STATUS_DOT_COLOR)


def compute_status_counts(df):
    if df.empty:
        return {}
    counts = df["Status"].value_counts()
    return dict(sorted(counts.items(), key=lambda kv: kv[1], reverse=True))


def bucket_for(hours_remaining, due_dt):
    if hours_remaining is None or pd.isna(hours_remaining) or due_dt is None:
        return BUCKET_OVER_1H
    minutes = hours_remaining * 60
    if minutes < 0:
        jira_tz = due_dt.tzinfo
        if due_dt.date() < datetime.now(jira_tz).date():
            return BUCKET_BREACHED_PAST
        return BUCKET_BREACHED_TODAY
    if minutes <= 15:
        return BUCKET_15
    if minutes <= 30:
        return BUCKET_30
    if minutes <= 45:
        return BUCKET_45
    return BUCKET_OVER_1H


def compute_time_buckets(df):
    buckets = {b: {"count": 0, "tickets": []} for b in BUCKET_ORDER}
    if df.empty:
        return buckets
    for _, row in df.iterrows():
        b = bucket_for(row["Hours Remaining"], row["Due Datetime"])
        buckets[b]["count"] += 1
        buckets[b]["tickets"].append({"key": row["Key"], "assignee": row["Assignee"]})
    return buckets


def jira_ticket_url(key):
    return f'{config.JIRA_BASE_URL.rstrip("/")}/browse/{key}'


def bucket_popup_html(tickets):
    if not tickets:
        return '<div class="bucket-popup-inner"><div class="bucket-popup-empty">No tickets</div></div>'
    items = "".join(
        f'<a class="bucket-popup-item" href="{html.escape(jira_ticket_url(t["key"]), quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">'
        f'<span class="bucket-popup-key">{html.escape(t["key"])}</span>'
        f'<span class="bucket-popup-assignee" title="{html.escape(t["assignee"])}">{html.escape(t["assignee"])}</span></a>'
        for t in tickets
    )
    return f'<div class="bucket-popup-inner">{items}</div>'


def resolved_breach_popup_html(tickets):
    if not tickets:
        return '<div class="bucket-popup-inner"><div class="bucket-popup-empty">No tickets</div></div>'
    items = "".join(
        f'<a class="bucket-popup-item" href="{html.escape(jira_ticket_url(t["key"]), quote=True)}" '
        f'target="_blank" rel="noopener noreferrer">'
        f'<span class="bucket-popup-key">{html.escape(t["key"])}</span>'
        f'<span class="bucket-popup-tag" style="background:{BUCKET_COLOR[t["timing"]]};">{html.escape(t["timing"])}</span>'
        f'<span class="bucket-popup-assignee" title="{html.escape(t["assignee"])}">{html.escape(t["assignee"])}</span></a>'
        for t in tickets
    )
    return f'<div class="bucket-popup-inner">{items}</div>'


_popup_counter = itertools.count()


def popup_row(row_inner_html, popup_inner_html, title):
    """A bucket row whose ticket popup opens on hover and then stays open until
    its close (x) button is clicked. Open/close is handled by POPUP_SCRIPT."""
    popup_id = f"popup-{next(_popup_counter)}"
    return (
        f'<div class="popup-wrap" data-popup-id="{popup_id}">'
        f'<div class="bucket-row has-popup">{row_inner_html}</div>'
        f'<div class="bucket-popup"><div class="bucket-popup-box">'
        f'<div class="bucket-popup-header"><span>{html.escape(title)}</span>'
        f'<span class="bucket-popup-close" title="Close">&times;</span></div>'
        f'{popup_inner_html}</div></div>'
        f'</div>'
    )


# Hovering a row opens its popup (closing any other open one); it then stays
# open until its x is clicked. The open popup's id is remembered so it's
# re-opened after a Streamlit rerun (e.g. the 1-minute auto-refresh) redraws it.
POPUP_SCRIPT = """
<script>
(function () {
  const win = window.parent && window.parent.document ? window.parent : window;
  const doc = win.document;
  if (win.__slaPopupsInstalled) return;
  win.__slaPopupsInstalled = true;
  win.__slaOpenPopup = null;

  function setOpen(id) {
    win.__slaOpenPopup = id;
    doc.querySelectorAll(".popup-wrap.open").forEach(function (w) {
      if (w.dataset.popupId !== id) w.classList.remove("open");
    });
    if (id) {
      const w = doc.querySelector('.popup-wrap[data-popup-id="' + id + '"]');
      if (w) openWrap(w);
    }
  }

  // Shows a popup, flipping it to align with the row's right edge if it
  // would otherwise run past the right side of the window.
  function openWrap(w) {
    w.classList.add("open");
    const pop = w.querySelector(".bucket-popup");
    if (!pop) return;
    pop.classList.remove("align-right");
    if (pop.getBoundingClientRect().right > doc.documentElement.clientWidth - 8) {
      pop.classList.add("align-right");
    }
  }

  // Row left under the pointer when a popup is closed; it must not instantly
  // re-open, so it's ignored until the pointer moves off it.
  let suppressRow = null;

  doc.addEventListener("mouseover", function (e) {
    const row = e.target.closest && e.target.closest(".has-popup");
    if (suppressRow && row !== suppressRow) suppressRow = null;
    if (!row || row === suppressRow) return;
    const wrap = row.closest(".popup-wrap");
    if (wrap && !wrap.classList.contains("open")) setOpen(wrap.dataset.popupId);
  });

  doc.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest(".bucket-popup-close")) {
      e.preventDefault();
      setOpen(null);
      const under = doc.elementFromPoint(e.clientX, e.clientY);
      suppressRow = under && under.closest ? under.closest(".has-popup") : null;
    }
  });

  new win.MutationObserver(function () {
    const id = win.__slaOpenPopup;
    if (!id) return;
    const w = doc.querySelector('.popup-wrap[data-popup-id="' + id + '"]');
    if (w && !w.classList.contains("open")) openWrap(w);
  }).observe(doc.body, { childList: true, subtree: true });
})();
</script>
"""


def escalated_tickets_with_links(df):
    if df.empty:
        return []
    escalated = df[df["Status"].str.strip().str.lower() == "escalated"]
    return [{"key": row["Key"], "links": row["Linked Issues"]} for _, row in escalated.iterrows()]


def escalated_links_popup_html(tickets):
    if not tickets:
        return '<div class="bucket-popup-inner"><div class="bucket-popup-empty">No escalated tickets</div></div>'
    blocks = []
    for t in tickets:
        link_count = len(t["links"])
        summary_text = f'{link_count} linked ticket{"s" if link_count != 1 else ""}' if link_count else "No linked tickets"
        blocks.append(
            f'<a class="bucket-popup-item" href="{html.escape(jira_ticket_url(t["key"]), quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">'
            f'<span class="bucket-popup-key">{html.escape(t["key"])}</span>'
            f'<span class="bucket-popup-assignee" title="{html.escape(summary_text)}">{html.escape(summary_text)}</span></a>'
        )
        blocks.extend(
            f'<a class="bucket-popup-item bucket-popup-sublink" href="{html.escape(jira_ticket_url(l["key"]), quote=True)}" '
            f'target="_blank" rel="noopener noreferrer">'
            f'<span class="bucket-popup-key">&#8618; {html.escape(l["key"])}</span>'
            f'<span class="bucket-popup-assignee" title="{html.escape(l["summary"])}">{html.escape(l["summary"])}</span></a>'
            for l in t["links"]
        )
    return f'<div class="bucket-popup-inner">{"".join(blocks)}</div>'


st.set_page_config(page_title="Jira Breach Monitor", page_icon="🔔", layout="wide")

AUTO_REFRESH_SECONDS = 60  # matches load_category_data/load_resolved_data cache ttl
st_autorefresh(interval=AUTO_REFRESH_SECONDS * 1000, key="dashboard_autorefresh")

st.markdown(
    f"""
    <style>
        #MainMenu, footer, header {{visibility: hidden;}}

        [data-testid="stAppViewContainer"] {{
            background:
                radial-gradient(circle at 0% 0%, rgba(124,58,237,0.22), transparent 45%),
                radial-gradient(circle at 100% 0%, rgba(31,111,235,0.18), transparent 45%),
                {SURFACE_PAGE};
        }}
        [data-testid="stMain"] {{ background: transparent; overflow-x: hidden; }}
        /* Keep the dashboard steady while Streamlit reruns (auto-refresh): no stale-element fade. */
        [data-stale="true"] {{ opacity: 1 !important; transition: none !important; }}
        html, body, [data-testid="stApp"], [data-testid="stAppViewContainer"] {{ overflow-x: hidden; }}

        .block-container, [data-testid="stMainBlockContainer"] {{
            background: {SURFACE_CARD};
            border: 1px solid rgba(255,255,255,0.07);
            border-radius: 20px;
            padding: 1.2rem 1.8rem 1.6rem 1.8rem;
            margin-top: 0.8rem;
            margin-bottom: 0.8rem;
            max-width: 1400px;
            box-shadow: 0 20px 60px rgba(0,0,0,0.45);
        }}
        [data-testid="stVerticalBlock"] {{ gap: 0.55rem; }}

        .app-header {{ display: flex; align-items: center; gap: 10px; }}
        .app-logo {{
            width: 36px; height: 36px; border-radius: 10px; flex-shrink: 0;
            background: linear-gradient(135deg, {ACCENT_FROM}, {ACCENT_TO});
            display: flex; align-items: center; justify-content: center; font-size: 17px;
            position: relative; animation: siren-pulse 2.2s ease-in-out infinite;
        }}
        @keyframes siren-pulse {{
            0%, 100% {{ box-shadow: 0 0 0 0 rgba(219,39,119,0.55); }}
            50% {{ box-shadow: 0 0 0 7px rgba(219,39,119,0); }}
        }}
        .app-logo svg {{ display: block; }}
        .app-title {{ font-size: 1.15rem; font-weight: 700; color: {TEXT_PRIMARY}; line-height: 1.1; }}
        .app-subtitle {{ font-size: 0.75rem; color: {TEXT_MUTED}; margin-top: 1px; }}
        .app-refreshed {{ font-size: 0.68rem; color: {TEXT_MUTED}; text-align: right; }}
        .header-spacer {{ height: 2px; }}

        .overall-summary {{
            display: flex; align-items: center; justify-content: center; gap: 18px;
            height: 100%; padding: 8px 20px;
            background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
            border-radius: 12px;
        }}
        .overall-stat {{ text-align: center; }}
        .overall-stat-value {{ font-size: 1.1rem; font-weight: 700; color: {TEXT_PRIMARY}; line-height: 1.1; }}
        .overall-stat-label {{
            font-size: 0.6rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
            color: {TEXT_MUTED}; margin-top: 2px;
        }}
        .overall-divider {{ width: 1px; height: 28px; background: rgba(255,255,255,0.12); }}

        .section-title {{
            font-size: 0.72rem; font-weight: 700; letter-spacing: 0.06em; text-transform: uppercase;
            color: {TEXT_SECONDARY}; background: rgba(255,255,255,0.04);
            border: 1px solid rgba(255,255,255,0.08); border-radius: 8px;
            padding: 7px 14px; margin: 6px 0 2px 0;
        }}

        .stat-card {{
            border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; overflow: hidden;
            background: {SURFACE_PANEL};
        }}
        .stat-card-head {{
            background: {HEADER_BG}; color: {TEXT_PRIMARY}; font-weight: 700; text-align: center;
            padding: 7px 8px 6px 8px; font-size: 0.82rem; min-height: 2.6rem;
            display: flex; align-items: center; justify-content: center;
            border-bottom: 3px solid transparent;
            border-image: linear-gradient(90deg, {ACCENT_FROM}, {ACCENT_TO}) 1;
        }}
        .stat-card-value {{
            text-align: center; padding: 12px 8px; font-size: 1.6rem; font-weight: 700; color: {TEXT_PRIMARY};
        }}

        .bucket-card {{
            border: 1px solid rgba(255,255,255,0.08); border-radius: 12px;
            background: {SURFACE_PANEL};
        }}
        .bucket-card-head {{
            background: {HEADER_BG}; color: {TEXT_PRIMARY}; font-weight: 700; text-align: center;
            padding: 7px 8px 6px 8px; font-size: 0.82rem; border-radius: 12px 12px 0 0; min-height: 2.6rem;
            display: flex; align-items: center; justify-content: center;
            border-bottom: 3px solid transparent;
            border-image: linear-gradient(90deg, {ACCENT_FROM}, {ACCENT_TO}) 1;
        }}

        .summary-card {{ display: flex; flex-direction: column; min-height: 172px; }}
        .summary-card .stat-card-value {{ flex: 1; display: flex; align-items: center; justify-content: center; padding: 4px 8px; }}
        .bucket-row {{
            position: relative;
            display: flex; align-items: center; justify-content: space-between;
            padding: 7px 14px; border-top: 1px solid rgba(255,255,255,0.06);
        }}
        .bucket-row:last-child {{ border-radius: 0 0 12px 12px; }}
        .bucket-row .dot {{ width: 8px; height: 8px; border-radius: 50%; display: inline-block; margin-right: 8px; }}
        .bucket-row .label {{ color: {TEXT_SECONDARY}; font-size: 0.78rem; display: flex; align-items: center; cursor: default; }}
        .bucket-row .value {{ color: {TEXT_PRIMARY}; font-weight: 700; font-size: 0.85rem; }}

        .bucket-sla-value {{ text-align: center; padding: 10px 8px 0 8px; font-size: 1.4rem; font-weight: 700; }}
        .bucket-sla-caption {{
            text-align: center; font-size: 0.62rem; font-weight: 700; letter-spacing: 0.05em;
            text-transform: uppercase; color: {TEXT_MUTED}; padding-bottom: 6px;
        }}

        .bucket-popup {{
            position: absolute; left: 0; top: 100%; z-index: 30;
            width: max-content; min-width: 100%; max-width: 420px;
            padding-top: 4px;
            display: none;  /* not just invisible: hidden popups must not widen the page */
        }}
        .bucket-popup-box {{
            background: {SURFACE_CARD}; border: 1px solid rgba(255,255,255,0.12); border-radius: 10px;
            box-shadow: 0 12px 30px rgba(0,0,0,0.5); padding: 6px;
        }}
        .bucket-popup-inner {{ max-height: 260px; overflow-y: auto; }}
        .bucket-popup-header {{
            display: flex; align-items: center; justify-content: space-between; gap: 12px;
            padding: 2px 4px 6px 10px; margin-bottom: 4px; border-bottom: 1px solid rgba(255,255,255,0.08);
            font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: {TEXT_MUTED};
        }}
        .bucket-popup-close {{
            cursor: pointer; font-size: 1.1rem; line-height: 1; padding: 2px 8px; border-radius: 6px;
            color: {TEXT_SECONDARY}; text-transform: none;
        }}
        .bucket-popup-close:hover {{ background: rgba(255,255,255,0.1); color: {TEXT_PRIMARY}; }}
        .popup-wrap {{ position: relative; }}
        .bucket-row.has-popup {{ cursor: pointer; }}
        .bucket-row.has-popup:hover {{ background: rgba(255,255,255,0.04); }}
        .bucket-row.has-popup .label {{ cursor: pointer; }}
        .popup-wrap.open .bucket-popup {{ display: block; z-index: 40; }}
        .bucket-popup.align-right {{ left: auto; right: 0; }}
        .bucket-popup-item {{
            display: flex; align-items: center; justify-content: space-between; gap: 14px;
            padding: 6px 10px; border-radius: 6px; text-decoration: none;
            font-size: 0.82rem; color: {TEXT_SECONDARY};
        }}
        .bucket-popup-item:hover {{ background: rgba(255,255,255,0.06); color: {TEXT_PRIMARY}; }}
        .bucket-popup-sublink {{ padding-left: 20px; font-size: 0.74rem; opacity: 0.9; }}
        .bucket-popup-tag {{
            font-size: 0.6rem; font-weight: 700; padding: 2px 7px; border-radius: 999px;
            color: white; white-space: nowrap; flex-shrink: 0;
        }}
        .bucket-popup-key {{ font-weight: 700; color: {TEXT_PRIMARY}; white-space: nowrap; flex-shrink: 0; }}
        .bucket-popup-assignee {{
            text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            min-width: 0; max-width: 220px;
        }}
        .bucket-popup-empty {{ padding: 8px; font-size: 0.8rem; color: {TEXT_MUTED}; text-align: center; }}

        button[data-baseweb="tab"] {{
            background-color: transparent; border-radius: 999px !important; padding: 6px 20px !important;
            color: {TEXT_MUTED} !important; margin-right: 4px;
        }}
        button[data-baseweb="tab"][aria-selected="true"] {{
            background: linear-gradient(135deg, {ACCENT_FROM}, {ACCENT_TO}) !important;
            color: white !important; font-weight: 600;
        }}
        div[data-baseweb="tab-list"] {{
            gap: 2px; border-bottom: none; background: rgba(255,255,255,0.04);
            border-radius: 999px; padding: 6px; width: fit-content;
        }}
        div[data-baseweb="tab-highlight"], div[data-baseweb="tab-border"] {{ display: none; }}

        div[data-testid="stDataFrame"] {{
            border: 1px solid rgba(255,255,255,0.07); border-radius: 10px; overflow: hidden;
        }}
    </style>
    """,
    unsafe_allow_html=True,
)
st.html(POPUP_SCRIPT, unsafe_allow_javascript=True)

ESCALATION_PATH_FIELD = "customfield_10883"
ESCALATION_PATH_OTHER_FIELD = "customfield_10884"


def escalation_path_value(fields):
    path_field = fields.get(ESCALATION_PATH_FIELD)
    path = path_field.get("value", "") if isinstance(path_field, dict) else (path_field or "")
    if path.strip().lower() == "other":
        other = fields.get(ESCALATION_PATH_OTHER_FIELD) or ""
        return f"Other: {other}" if other else "Other"
    return path


def extract_linked_issues(fields):
    """Pulls the linked issues (any link type/direction) off a Jira issue's
    issuelinks field into a flat, de-duplicated [{key, summary}] list."""
    links = fields.get("issuelinks") or []
    seen = set()
    result = []
    for link in links:
        linked = link.get("outwardIssue") or link.get("inwardIssue")
        if not linked:
            continue
        key = linked.get("key")
        if not key or key in seen:
            continue
        seen.add(key)
        result.append({"key": key, "summary": (linked.get("fields") or {}).get("summary", "")})
    return result


@st.cache_data(ttl=AUTO_REFRESH_SECONDS, show_spinner=False)  # a spinner per load pushes the page down -> visible shaking
def load_category_data(category, cat_config):
    fields = ["summary", "status", "assignee", "priority", "created", "issuelinks",
              ESCALATION_PATH_FIELD, ESCALATION_PATH_OTHER_FIELD]
    if cat_config["breach_type"] == "sla_from_created":
        fields.append(cat_config["severity_field"])
    elif cat_config["breach_type"] == "date_field":
        fields.append(cat_config["breach_field"])
    issues = jira_client.search_issues(cat_config["jql"], fields=fields)

    rows = []
    for issue in issues:
        f = issue["fields"]
        result = breach_logic.evaluate(issue, cat_config)
        rows.append({
            "Key": issue["key"],
            "Summary": f.get("summary", ""),
            "Status": (f.get("status") or {}).get("name", ""),
            "Priority": (f.get("priority") or {}).get("name", ""),
            "Severity": result["severity"] or "N/A",
            "Assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
            "Created": f.get("created", ""),
            "Due": result["due_date"].strftime("%Y-%m-%d %H:%M") if result["due_date"] else "N/A",
            "Due Datetime": result["due_date"],
            "Hours Remaining": result["hours_remaining"],
            "Breach Status": result["status"],
            "Escalation Path": escalation_path_value(f),
            "Linked Issues": extract_linked_issues(f),
        })
    return pd.DataFrame(rows)


@st.cache_data(ttl=AUTO_REFRESH_SECONDS, show_spinner=False)  # a spinner per load pushes the page down -> visible shaking
def load_resolved_data(category, cat_config):
    fields = ["summary", "status", "assignee", "created", "resolutiondate", cat_config["severity_field"]]
    issues = jira_client.search_issues(cat_config["resolved_jql"], fields=fields)

    rows = []
    for issue in issues:
        f = issue["fields"]
        result = breach_logic.evaluate_resolution(issue, cat_config)
        rows.append({
            "Key": issue["key"],
            "Summary": f.get("summary", ""),
            "Status": (f.get("status") or {}).get("name", ""),
            "Severity": result["severity"] or "N/A",
            "Assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
            "Resolved Status": result["status"],
            "Due Datetime": result["due_date"],
        })
    return pd.DataFrame(rows)


def resolved_breach_timing(due_dt):
    """Classifies a breached resolved ticket as having missed its SLA due date
    today or on an earlier day, same day-boundary rule as the open-ticket
    Breached (Past)/Breached Today buckets."""
    if due_dt is None:
        return BUCKET_BREACHED_TODAY
    jira_tz = due_dt.tzinfo
    if due_dt.date() < datetime.now(jira_tz).date():
        return BUCKET_BREACHED_PAST
    return BUCKET_BREACHED_TODAY


def resolved_tickets_by_status(df, status):
    if df.empty:
        return []
    matched = df[df["Resolved Status"] == status]
    tickets = []
    for _, row in matched.iterrows():
        ticket = {"key": row["Key"], "assignee": row["Assignee"]}
        if status == breach_logic.STATUS_BREACHED:
            ticket["timing"] = resolved_breach_timing(row["Due Datetime"])
        tickets.append(ticket)
    return tickets


def compute_resolved_counts(df):
    if df.empty:
        return {"within_sla": 0, "breached": 0, "pct": None}
    counts = df["Resolved Status"].value_counts()
    within_sla = int(counts.get(breach_logic.STATUS_OK, 0))
    breached = int(counts.get(breach_logic.STATUS_BREACHED, 0))
    total = within_sla + breached
    pct = round(within_sla / total * 100, 1) if total else None
    return {"within_sla": within_sla, "breached": breached, "pct": pct}


def sla_pct_color(pct):
    if pct is None:
        return TEXT_MUTED
    if pct >= 90:
        return COLOR_GOOD
    if pct >= 70:
        return COLOR_WARNING
    return COLOR_CRITICAL


def style_status(val):
    colors = {
        breach_logic.STATUS_BREACHED: f"background-color: #4C1D1D; color: {COLOR_CRITICAL}; font-weight: 600",
        breach_logic.STATUS_WARNING: f"background-color: #4A3A0F; color: {COLOR_WARNING}; font-weight: 600",
        breach_logic.STATUS_OK: "background-color: #14321F; color: #86EFAC; font-weight: 600",
        breach_logic.STATUS_UNKNOWN: "background-color: #2D333B; color: #C9D1D9; font-weight: 600",
    }
    return colors.get(val, "")


# ---------------------------------------------------------------------------
# Fetch all category data once (shared by every section below)
# ---------------------------------------------------------------------------
category_data = {}
category_errors = {}
resolved_data = {}
resolved_errors = {}
for category, cat_config in config.CATEGORIES.items():
    try:
        category_data[category] = load_category_data(category, cat_config)
    except Exception as exc:
        category_errors[category] = exc
    try:
        resolved_data[category] = load_resolved_data(category, cat_config)
    except Exception as exc:
        resolved_errors[category] = exc

for category, exc in category_errors.items():
    st.error(f"Failed to load {category}: {exc}")
for category, exc in resolved_errors.items():
    st.error(f"Failed to load resolved tickets for {category}: {exc}")

ok_categories = {c: df for c, df in category_data.items() if c not in category_errors}

# ---------------------------------------------------------------------------
# Sidebar: chat assistant (Claude, tool use over the data already loaded above)
# Disabled for now (no ANTHROPIC_API_KEY set up yet) -- re-enable by
# uncommenting once Anthropic Console billing + a key are in place.
# ---------------------------------------------------------------------------
# with st.sidebar:
#     st.markdown("### Ask about your tickets")
#     if not config.ANTHROPIC_API_KEY:
#         st.caption("Set ANTHROPIC_API_KEY in config.py to enable this.")
#     else:
#         if "ai_display" not in st.session_state:
#             st.session_state.ai_display = []
#         if "ai_history" not in st.session_state:
#             st.session_state.ai_history = []
#
#         for turn in st.session_state.ai_display:
#             with st.chat_message(turn["role"]):
#                 st.markdown(turn["text"])
#
#         question = st.chat_input("e.g. what's breaching in B2B right now?")
#         if question:
#             with st.chat_message("user"):
#                 st.markdown(question)
#             st.session_state.ai_display.append({"role": "user", "text": question})
#
#             with st.chat_message("assistant"):
#                 with st.spinner("Thinking..."):
#                     try:
#                         answer, updated_history = ai_assistant.ask(
#                             question, st.session_state.ai_history, category_data, resolved_data
#                         )
#                     except Exception as exc:
#                         answer = f"Sorry, something went wrong: {exc}"
#                         updated_history = st.session_state.ai_history
#                 st.markdown(answer)
#             st.session_state.ai_history = updated_history
#             st.session_state.ai_display.append({"role": "assistant", "text": answer})

# ---------------------------------------------------------------------------
# Header: title, overall resolved/SLA summary, refresh button
# ---------------------------------------------------------------------------
overall_within_sla = 0
overall_breached = 0
for category, df in resolved_data.items():
    if category in resolved_errors:
        continue
    counts = compute_resolved_counts(df)
    overall_within_sla += counts["within_sla"]
    overall_breached += counts["breached"]
overall_total_resolved = overall_within_sla + overall_breached
overall_pct = round(overall_within_sla / overall_total_resolved * 100, 1) if overall_total_resolved else None
overall_pct_display = "—" if overall_pct is None else f"{overall_pct:.0f}%"

header_col1, header_col2, header_col3 = st.columns([4, 2.4, 1.3])
with header_col1:
    st.markdown(
        '<div class="app-header"><div class="app-logo">'
        '<svg viewBox="0 0 24 24" width="19" height="19" fill="white">'
        '<path d="M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2zm6-6v-5c0-3.07-1.64-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.63 5.36 6 7.92 6 11v5l-2 2v1h16v-1l-2-2zm-9.44-8.6l-1.06-1.06C6.09 5.64 5 8.51 5 11h1.5c0-2.42.72-4.78 2.06-6.6zM19.5 11H21c0-2.49-1.09-5.36-2.5-7.66l-1.06 1.06C18.78 6.22 19.5 8.58 19.5 11z"/>'
        '</svg>'
        '</div><div>'
        '<div class="app-title">JIRA - SLA Tracker & Notifier</div>'
        '<div class="app-subtitle">Real-time ticket monitoring across Retail, SAC, B2B and Order Fallout queues</div>'
        '</div></div>',
        unsafe_allow_html=True,
    )
with header_col2:
    st.markdown(
        '<div class="overall-summary">'
        f'<div class="overall-stat"><div class="overall-stat-value">{overall_total_resolved}</div>'
        '<div class="overall-stat-label">Resolved Today</div></div>'
        '<div class="overall-divider"></div>'
        f'<div class="overall-stat"><div class="overall-stat-value" style="color:{sla_pct_color(overall_pct)};">'
        f'{overall_pct_display}</div><div class="overall-stat-label">Overall SLA</div></div>'
        '</div>',
        unsafe_allow_html=True,
    )
with header_col3:
    if st.button("🔄  Refresh now", use_container_width=True):
        st.cache_data.clear()
        st.rerun()
    st.markdown(
        f'<div class="app-refreshed">Last refreshed: {datetime.now().strftime("%Y-%m-%d %H:%M:%S")}'
        f' &middot; auto-refreshes every {AUTO_REFRESH_SECONDS // 60} min</div>',
        unsafe_allow_html=True,
    )

st.markdown('<div class="header-spacer"></div>', unsafe_allow_html=True)

# ---------------------------------------------------------------------------
# Section: Total Open Tickets  |  Total Resolved Tickets - Today
# ---------------------------------------------------------------------------
open_section, resolved_section = st.columns(2, gap="large")

with open_section:
    st.markdown('<div class="section-title">Total Open Tickets</div>', unsafe_allow_html=True)
    cols = st.columns(len(config.CATEGORIES))
    for col, category in zip(cols, config.CATEGORIES.keys()):
        value = "—" if category in category_errors else len(category_data[category])
        col.markdown(
            f'<div class="stat-card summary-card"><div class="stat-card-head">{category}</div>'
            f'<div class="stat-card-value">{value}</div></div>',
            unsafe_allow_html=True,
        )

with resolved_section:
    st.markdown('<div class="section-title">Total Resolved Tickets &mdash; Today</div>', unsafe_allow_html=True)
    cols = st.columns(len(config.CATEGORIES))
    for col, category in zip(cols, config.CATEGORIES.keys()):
        if category in resolved_errors:
            col.markdown(
                f'<div class="bucket-card summary-card"><div class="bucket-card-head">{category}</div>'
                f'<div class="bucket-row"><span class="label">Error loading</span></div></div>',
                unsafe_allow_html=True,
            )
            continue

        counts = compute_resolved_counts(resolved_data[category])
        pct_display = "—" if counts["pct"] is None else f'{counts["pct"]:.0f}%'
        breached_tickets = resolved_tickets_by_status(resolved_data[category], breach_logic.STATUS_BREACHED)
        rows_html = (
            f'<div class="bucket-sla-value" style="color:{sla_pct_color(counts["pct"])};">{pct_display}</div>'
            f'<div class="bucket-sla-caption">SLA Compliance</div>'
            f'<div class="bucket-row"><span class="label">'
            f'<span class="dot" style="background:{COLOR_GOOD};"></span>Within SLA</span>'
            f'<span class="value">{counts["within_sla"]}</span></div>'
            + popup_row(
                f'<span class="label"><span class="dot" style="background:{COLOR_CRITICAL};"></span>Breached</span>'
                f'<span class="value">{counts["breached"]}</span>',
                resolved_breach_popup_html(breached_tickets),
                f"{category} — Breached",
            )
        )
        col.markdown(
            f'<div class="bucket-card summary-card"><div class="bucket-card-head">{category}</div>{rows_html}</div>',
            unsafe_allow_html=True,
        )

# ---------------------------------------------------------------------------
# Section: Time to Resolve - Open Tickets
# ---------------------------------------------------------------------------
st.markdown('<div class="section-title">Time to Resolve &mdash; Open Tickets</div>', unsafe_allow_html=True)
cols = st.columns(len(config.CATEGORIES))
for col, category in zip(cols, config.CATEGORIES.keys()):
    if category in category_errors:
        col.markdown(
            f'<div class="bucket-card"><div class="bucket-card-head">{category}</div>'
            f'<div class="bucket-row"><span class="label">Error loading</span></div></div>',
            unsafe_allow_html=True,
        )
        continue

    buckets = compute_time_buckets(category_data[category])
    rows_html = "".join(
        popup_row(
            f'<span class="label"><span class="dot" style="background:{BUCKET_COLOR[b]};"></span>{b}</span>'
            f'<span class="value">{buckets[b]["count"]}</span>',
            bucket_popup_html(buckets[b]["tickets"]),
            f"{category} — {b}",
        )
        for b in BUCKET_ORDER
    )
    col.markdown(
        f'<div class="bucket-card"><div class="bucket-card-head">{category}</div>{rows_html}</div>',
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Section: Tickets by Status
# ---------------------------------------------------------------------------
st.markdown('<div class="section-title">Total Open Tickets by Status</div>', unsafe_allow_html=True)
cols = st.columns(len(config.CATEGORIES))
for col, category in zip(cols, config.CATEGORIES.keys()):
    if category in category_errors:
        col.markdown(
            f'<div class="bucket-card"><div class="bucket-card-head">{category}</div>'
            f'<div class="bucket-row"><span class="label">Error loading</span></div></div>',
            unsafe_allow_html=True,
        )
        continue

    status_counts = compute_status_counts(category_data[category])
    if not status_counts:
        rows_html = '<div class="bucket-row"><span class="label">No open tickets</span></div>'
    else:
        escalated_tickets = escalated_tickets_with_links(category_data[category])
        rows_html = "".join(
            popup_row(
                f'<span class="label"><span class="dot" style="background:{status_dot_color(status)};"></span>{status}</span>'
                f'<span class="value">{count}</span>',
                escalated_links_popup_html(escalated_tickets),
                f"{category} — Escalated",
            )
            if status.strip().lower() == "escalated"
            else (
                f'<div class="bucket-row"><span class="label">'
                f'<span class="dot" style="background:{status_dot_color(status)};"></span>{status}</span>'
                f'<span class="value">{count}</span></div>'
            )
            for status, count in status_counts.items()
        )
    col.markdown(
        f'<div class="bucket-card"><div class="bucket-card-head">{category}</div>{rows_html}</div>',
        unsafe_allow_html=True,
    )

# ---------------------------------------------------------------------------
# Section: Escalated Tickets (path + severity)
# ---------------------------------------------------------------------------
st.markdown('<div class="section-title">Escalated Tickets</div>', unsafe_allow_html=True)
escalated_frames = []
for category, df in ok_categories.items():
    if df.empty:
        continue
    escalated = df[df["Status"].str.strip().str.lower() == "escalated"].copy()
    if not escalated.empty:
        escalated.insert(0, "Category", category)
        escalated_frames.append(escalated)

if escalated_frames:
    escalated_df = pd.concat(escalated_frames, ignore_index=True)
    st.dataframe(
        escalated_df[["Key", "Category", "Summary", "Severity", "Escalation Path", "Assignee", "Due"]],
        use_container_width=True,
        height=min(400, 60 + 35 * len(escalated_df)),
    )
else:
    st.info("No tickets are currently escalated across any queue.")

# ---------------------------------------------------------------------------
# Section: Ticket Details (per-category, filterable)
# ---------------------------------------------------------------------------
st.markdown('<div class="section-title">Ticket Details</div>', unsafe_allow_html=True)

tabs = st.tabs(list(config.CATEGORIES.keys()))
for tab, (category, cat_config) in zip(tabs, config.CATEGORIES.items()):
    with tab:
        if category in category_errors:
            st.error(f"Failed to load {category}: {category_errors[category]}")
            continue
        df = category_data[category]

        if df.empty:
            st.info("No tickets matched this query.")
            continue

        breach_filter = st.multiselect(
            "Filter by breach status",
            options=STATUS_ORDER,
            default=[breach_logic.STATUS_WARNING, breach_logic.STATUS_BREACHED],
            key=f"filter_{category}",
        )
        shown = df[df["Breach Status"].isin(breach_filter)] if breach_filter else df

        st.dataframe(
            shown.style.map(style_status, subset=["Breach Status"]),
            use_container_width=True,
            height=500,
        )

# ---------------------------------------------------------------------------
# Section: Productivity by Assignee — Today
# ---------------------------------------------------------------------------
st.markdown('<div class="section-title">Productivity by Assignee &mdash; Today</div>', unsafe_allow_html=True)

category_names = list(config.CATEGORIES.keys())
team_filter = st.multiselect(
    "Filter by team",
    options=category_names,
    default=category_names,
    key="productivity_team_filter",
)
selected_categories = [cat for cat in category_names if cat in team_filter]

per_assignee_open = defaultdict(lambda: defaultdict(int))
for category, df in category_data.items():
    if category not in selected_categories or category in category_errors or df.empty:
        continue
    for assignee, count in df["Assignee"].value_counts().items():
        per_assignee_open[assignee][category] = int(count)

per_assignee_resolved = defaultdict(lambda: defaultdict(int))
for category, df in resolved_data.items():
    if category not in selected_categories or category in resolved_errors or df.empty:
        continue
    for assignee, count in df["Assignee"].value_counts().items():
        per_assignee_resolved[assignee][category] = int(count)

assignees = set(per_assignee_open) | set(per_assignee_resolved)

if not selected_categories:
    st.info("Select at least one team to see productivity data.")
elif not assignees:
    st.info("No open or resolved tickets to report on.")
else:
    open_cols = [f"{cat} (Open)" for cat in selected_categories]
    resolved_cols = [f"{cat} (Resolved)" for cat in selected_categories]

    rows = []
    for assignee in assignees:
        row = {"Assignee": assignee}
        total_open = 0
        total_resolved = 0
        for cat in selected_categories:
            row[f"{cat} (Open)"] = per_assignee_open[assignee].get(cat, 0)
            row[f"{cat} (Resolved)"] = per_assignee_resolved[assignee].get(cat, 0)
            total_open += row[f"{cat} (Open)"]
            total_resolved += row[f"{cat} (Resolved)"]
        row["Total Open"] = total_open
        row["Total Resolved Today"] = total_resolved
        rows.append(row)

    columns = ["Assignee", *open_cols, *resolved_cols, "Total Open", "Total Resolved Today"]
    productivity_df = pd.DataFrame(rows, columns=columns)
    productivity_df = productivity_df.sort_values("Total Open", ascending=True).reset_index(drop=True)

    grand_total = {"Assignee": "Grand Total"}
    for col in [*open_cols, *resolved_cols, "Total Open", "Total Resolved Today"]:
        grand_total[col] = int(productivity_df[col].sum())
    display_df = pd.concat([productivity_df, pd.DataFrame([grand_total])], ignore_index=True)

    st.dataframe(display_df, use_container_width=True, height=min(500, 60 + 35 * len(display_df)))

    excel_buffer = io.BytesIO()
    with pd.ExcelWriter(excel_buffer, engine="openpyxl") as writer:
        display_df.to_excel(writer, sheet_name="Productivity", index=False)
        ws = writer.sheets["Productivity"]
        for col_idx, col_name in enumerate(display_df.columns, start=1):
            max_len = max(len(str(col_name)), display_df[col_name].astype(str).map(len).max()) + 4
            ws.column_dimensions[ws.cell(row=1, column=col_idx).column_letter].width = max_len
        bold = Font(bold=True)
        for cell in ws[1]:
            cell.font = bold
        for cell in ws[ws.max_row]:
            cell.font = bold

    st.download_button(
        "⬇️ Download as Excel",
        data=excel_buffer.getvalue(),
        file_name=f"Productivity_Report_{datetime.now().strftime('%Y-%m-%d')}.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )
