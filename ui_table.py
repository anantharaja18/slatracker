"""
Styled HTML tables shared by the dashboard and its pages: dark panel, sticky
header, hover rows, and cells styled by what they hold -- Jira keys as links,
status / severity / SLA as colored pills, counts right-aligned with zeros
faded, long text truncated with the full text on hover, an optional pinned
totals row. Include TABLE_CSS once per page, then st.markdown(html_table(df)).
"""

import html

import pandas as pd

import config

TEXT_PRIMARY = "#F5F5F7"
TEXT_SECONDARY = "#C3C2B7"
TEXT_MUTED = "#898781"
SURFACE_PANEL = "#1a1a24"
HEADER_BG = "#20202c"
COLOR_GOOD = "#0ca30c"
COLOR_WARNING = "#fab219"
COLOR_CRITICAL = "#d03b3b"
COLOR_NEUTRAL = "#898781"
HEAT_RGB = "57,135,229"  # sequential blue for count heatmaps

# Jira workflow status -> dot color (shared with the dashboard's status cards).
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

SEVERITY_TAG_COLORS = {  # (background, text)
    "Sev-1": ("#7c4dff", "white"),
    "Sev-2": ("#0e7fa6", "white"),
}

# SLA / breach outcome -> color (dashboard breach statuses and resolved SLA).
SLA_COLOR = {
    "ok": COLOR_GOOD, "within sla": COLOR_GOOD,
    "about to breach": COLOR_WARNING,
    "breached": COLOR_CRITICAL, "resolved late": COLOR_CRITICAL,
}

# Column name -> cell style, when not given explicitly.
AUTO_KIND = {
    "key": "key", "ticket": "key",
    "status": "status", "jira status": "status",
    "severity": "severity",
    "sla": "sla", "breach status": "sla",
    "sla %": "pct",
    "linked": "links", "linked tickets": "links",
    "summary": "long", "error": "long", "driver": "long",
}

TABLE_CSS = f"""
    .ut-wrap {{ overflow: auto; border: 1px solid rgba(255,255,255,0.08); border-radius: 12px; background: {SURFACE_PANEL}; }}
    .ut-table {{ width: 100%; border-collapse: separate; border-spacing: 0; font-size: 0.82rem;
                 color: {TEXT_SECONDARY}; font-variant-numeric: tabular-nums; }}
    .ut-table th {{
        position: sticky; top: 0; z-index: 2; background: {HEADER_BG}; color: {TEXT_MUTED};
        font-size: 0.64rem; font-weight: 700; letter-spacing: 0.05em; text-transform: uppercase;
        text-align: left; padding: 10px 12px; white-space: nowrap; border-bottom: 1px solid rgba(255,255,255,0.1);
    }}
    .ut-table td {{ padding: 7px 12px; border-top: 1px solid rgba(255,255,255,0.05); vertical-align: middle; }}
    .ut-table tbody tr:hover td {{ background: rgba(255,255,255,0.035); }}
    .ut-table .ut-num {{ text-align: right; white-space: nowrap; }}
    .ut-table th.ut-num {{ text-align: right; }}
    .ut-zero {{ color: rgba(255,255,255,0.22); }}
    .ut-nowrap {{ white-space: nowrap; }}
    .ut-long {{ max-width: 460px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; color: {TEXT_PRIMARY}; }}
    .ut-sno {{ color: {TEXT_MUTED}; text-align: right; width: 1%; white-space: nowrap; }}
    .ut-table a.ut-key {{ color: #6ea8ff; font-weight: 700; text-decoration: none; white-space: nowrap; }}
    .ut-table a.ut-key:hover {{ text-decoration: underline; }}
    .ut-table a.ut-chip {{
        display: inline-block; margin: 1px 4px 1px 0; padding: 1px 8px; border-radius: 999px;
        background: rgba(110,168,255,0.12); color: #9cc3ff; font-size: 0.72rem; font-weight: 600;
        text-decoration: none; white-space: nowrap;
    }}
    .ut-table a.ut-chip:hover {{ background: rgba(110,168,255,0.25); color: {TEXT_PRIMARY}; }}
    .ut-status {{
        display: inline-flex; align-items: center; gap: 6px; white-space: nowrap; font-size: 0.74rem; font-weight: 600;
        padding: 1px 9px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.14); color: {TEXT_SECONDARY};
    }}
    .ut-status i {{ width: 7px; height: 7px; border-radius: 50%; display: inline-block; }}
    .ut-tag {{ font-size: 0.66rem; font-weight: 700; padding: 2px 8px; border-radius: 999px; white-space: nowrap; }}
    .ut-pill {{
        display: inline-block; min-width: 54px; text-align: center; padding: 1px 9px; border-radius: 999px;
        font-size: 0.74rem; font-weight: 700; border: 1px solid; white-space: nowrap;
    }}
    .ut-table tr.ut-total td {{
        position: sticky; bottom: 0; z-index: 1; background: {HEADER_BG}; color: {TEXT_PRIMARY}; font-weight: 700;
        border-top: 1px solid rgba(255,255,255,0.14);
    }}
    .ut-caption {{ font-size: 0.7rem; color: {TEXT_MUTED}; margin: 6px 2px 0 2px; }}
"""


def jira_url(key):
    return f'{config.JIRA_BASE_URL.rstrip("/")}/browse/{key}'


def status_color(status):
    return STATUS_DOT_COLOR.get((status or "").strip().lower(), DEFAULT_STATUS_DOT_COLOR)


def pct_color(pct):
    """Dashboard SLA thresholds: >= 90 green, >= 70 amber, else red."""
    return COLOR_GOOD if pct >= 90 else COLOR_WARNING if pct >= 70 else COLOR_CRITICAL


def _blank(v):
    return v is None or (isinstance(v, float) and pd.isna(v)) or (isinstance(v, str) and not v.strip())


def _pill(text, color):
    return (f'<span class="ut-pill" style="color:{color};border-color:{color}66;background:{color}1f;">'
            f'{html.escape(text)}</span>')


def _cell(kind, v, heat_max=None, decimals=1):
    if _blank(v):
        return '<span class="ut-zero">—</span>'
    if kind == "key":
        return f'<a class="ut-key" href="{html.escape(jira_url(v), quote=True)}" target="_blank" rel="noopener">{html.escape(str(v))}</a>'
    if kind == "links":
        keys = [k.strip() for k in str(v).split(",") if k.strip()]
        return "".join(f'<a class="ut-chip" href="{html.escape(jira_url(k), quote=True)}" target="_blank" '
                       f'rel="noopener">{html.escape(k)}</a>' for k in keys)
    if kind == "status":
        return f'<span class="ut-status"><i style="background:{status_color(v)};"></i>{html.escape(str(v))}</span>'
    if kind == "severity":
        bg, fg = SEVERITY_TAG_COLORS.get(str(v), (DEFAULT_STATUS_DOT_COLOR, TEXT_PRIMARY))
        return f'<span class="ut-tag" style="background:{bg};color:{fg};">{html.escape(str(v))}</span>'
    if kind == "sla":
        s = str(v)
        return _pill(s, SLA_COLOR.get(s.strip().lower(), COLOR_NEUTRAL))
    if kind == "pct":
        return _pill(f"{float(v):.1f}%", pct_color(float(v)))
    if kind in ("num", "heat"):
        f = float(v)
        if f == 0:
            return '<span class="ut-zero">—</span>'
        text = f"{int(f):,}" if f.is_integer() else f"{f:,.{decimals}f}"
        if kind == "heat" and heat_max:
            alpha = 0.08 + 0.55 * (f / heat_max)
            return (f'<span style="display:inline-block;min-width:34px;padding:1px 7px;border-radius:6px;'
                    f'background:rgba({HEAT_RGB},{alpha:.2f});color:{TEXT_PRIMARY};">{text}</span>')
        return text
    if kind == "long":
        s = str(v)
        return f'<div class="ut-long" title="{html.escape(s, quote=True)}">{html.escape(s)}</div>'
    return html.escape(str(v))


def _kind_for(col, series):
    name = str(col).strip().lower()
    if name in ("s.no", "#"):
        return "sno"
    if name in AUTO_KIND:
        return AUTO_KIND[name]
    if name.endswith("sla %"):
        return "pct"
    if pd.api.types.is_numeric_dtype(series) and not pd.api.types.is_bool_dtype(series):
        return "num"
    return "text"


def html_table(df, kinds=None, max_height=520, max_rows=1000, total_row=False, index=False,
               caption=None, decimals=1, heat_by="column"):
    """df -> styled HTML table. kinds overrides the per-column style
    ("key", "links", "status", "severity", "sla", "pct", "num", "heat",
    "long", "text", "sno"); others are picked from the column name / dtype.
    total_row pins the last row as a totals footer. At most max_rows rows are
    drawn (a note says so -- the Excel download has them all). heat_by
    "row" shades each row against its own maximum (e.g. a driver's weeks)."""
    if index:
        df = df.reset_index()
    kinds = dict(kinds or {})
    cols = list(df.columns)
    col_kind = {c: kinds.get(c) or _kind_for(c, df[c]) for c in cols}
    heat_cols = [c for c in cols if col_kind[c] == "heat"]
    heat_max = {c: (pd.to_numeric(df[c], errors="coerce").max() or 0) for c in heat_cols}

    body_df, total = (df.iloc[:-1], df.iloc[-1]) if total_row and len(df) else (df, None)
    shown = body_df.head(max_rows)

    def th(c):
        cls = ' class="ut-num"' if col_kind[c] in ("num", "heat", "pct", "sno") else ""
        return f"<th{cls}>{html.escape(str(c))}</th>"

    def td(c, v, in_total=False, row_max=None):
        k = col_kind[c]
        if k == "sno":
            return f'<td class="ut-sno">{html.escape(str(v))}</td>'
        # Everything but long text and link lists stays on one line.
        cls = "ut-num" if k in ("num", "heat", "pct") else ("" if k in ("long", "links") else "ut-nowrap")
        scale = row_max if heat_by == "row" else heat_max.get(c)
        content = html.escape(str(v)) if in_total and k in ("long", "text") else _cell(
            "num" if (in_total and k == "heat") else k, v, scale, decimals)
        return f'<td class="{cls}">{content}</td>' if cls else f"<td>{content}</td>"

    def row_html(r):
        row_max = pd.to_numeric(r[heat_cols], errors="coerce").max() if heat_cols and heat_by == "row" else None
        return "<tr>" + "".join(td(c, r[c], row_max=row_max or None) for c in cols) + "</tr>"

    rows = "".join(row_html(r) for _, r in shown.iterrows())
    if total is not None:
        rows += '<tr class="ut-total">' + "".join(td(c, total[c], in_total=True) for c in cols) + "</tr>"
    note = []
    if len(body_df) > max_rows:
        note.append(f"Showing the first {max_rows:,} of {len(body_df):,} rows — the Excel download has them all.")
    if caption:
        note.append(caption)
    note_html = f'<div class="ut-caption">{" ".join(html.escape(n) for n in note)}</div>' if note else ""
    return (f'<div class="ut-wrap" style="max-height:{max_height}px;"><table class="ut-table">'
            f'<thead><tr>{"".join(th(c) for c in cols)}</tr></thead><tbody>{rows}</tbody></table></div>{note_html}')
