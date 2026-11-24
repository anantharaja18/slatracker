"""
Shared SLA-by-severity view: the HTML table, the PNG image and the styles used
by the dashboard and by the Resolved Tickets History page.

Input is a DataFrame with one row per project (see config.PROJECT_GROUPS) plus
the Total row (config.TOTAL_LABEL). For each severity in config.SLA_MINUTES
(e.g. Sev-1, Sev-2) it has these columns:
    "<sev> Resolved", "<sev> Within SLA", "<sev> Resolved late", "<sev> Open breached", "<sev> SLA %"
The project layout (project_html / project_png / project_summary_frame) shows
SLA Met (= within SLA), SLA not Met (= resolved late + open breached), SLA Met %
per severity and Overall SLA % per row.
"""

import html
import io

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

import config
from ui_common import (ACCENT_FROM, ACCENT_TO, COLOR_CRITICAL, COLOR_GOOD, COLOR_WARNING, HEADER_BG,
                       TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY)

# Same team colours as the dashboard.
TEAM_COLOR = {"Retail": "#3987e5", "SAC": "#d95926", "B2B": "#199e70", "Order Fallout": "#c98500"}

CSS = f"""
    .asg-wrap {{ max-height: 560px; overflow: auto; border: 1px solid rgba(255,255,255,0.08);
                 border-radius: 12px; background: #1a1a24; }}
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
    .asg-popups {{ overflow: visible; max-height: none; }}
    .asg-table .asg-popup-cell {{ display: inline-block; cursor: pointer; }}
    .asg-table .popup-wrap {{ display: inline-block; }}
    .asg-name {{ display: flex; align-items: center; gap: 10px; color: {TEXT_PRIMARY}; font-weight: 600; }}
    .asg-zero {{ color: rgba(255,255,255,0.22); }}
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
        background: {HEADER_BG}; color: {TEXT_PRIMARY}; font-weight: 700;
        border-top: 1px solid rgba(255,255,255,0.14);
    }}
    .asg-legend {{ font-size: 0.7rem; color: {TEXT_MUTED}; margin: 6px 2px 0 2px; }}
    .asg-legend b {{ font-weight: 700; }}
"""

# Column classes for the count cells, by metric name.
METRIC_CLASS = {"Within SLA": "asg-good", "Breached SLA": "asg-bad"}


def metric_value(r, sev, metric):
    """One count for a team row and severity. Breached SLA = resolved late + open
    breached (open breached counts only when the table has that column)."""
    if metric == "Resolved":
        # Total Resolved = Within SLA + Breached SLA (resolved late + open breached)
        return int(r[f"{sev} Within SLA"]) + metric_value(r, sev, "Breached SLA")
    if metric == "Breached SLA":
        open_col = f"{sev} Open breached"
        return int(r[f"{sev} Resolved late"]) + (int(r[open_col]) if open_col in r.index else 0)
    return int(r[f"{sev} {metric}"])


def pct_color(pct):
    """Same thresholds as the dashboard's SLA %."""
    if pct is None or pd.isna(pct):
        return TEXT_MUTED
    return COLOR_GOOD if pct >= 90 else COLOR_WARNING if pct >= 70 else COLOR_CRITICAL


def sla_cell(pct):
    """SLA % as a green / amber / red pill plus a bar."""
    if pct is None or pd.isna(pct):
        return '<span class="asg-zero">—</span>'
    color = pct_color(pct)
    return (f'<span class="asg-sla"><span class="asg-slabar"><span style="width:{min(pct, 100):.0f}%;'
            f'background:{color};"></span></span><span class="asg-pill" style="color:{color};'
            f'border-color:{color}66;background:{color}1f;">{pct:.1f}%</span></span>')


def overall_pct(r):
    """SLA % across all severities for one team row: within / (within + breached)."""
    within = sum(int(r[f"{sev} Within SLA"]) for sev in config.SLA_MINUTES)
    breached = sum(metric_value(r, sev, "Breached SLA") for sev in config.SLA_MINUTES)
    judged = within + breached
    return round(within / judged * 100, 1) if judged else None


def count_cell(v, cls=""):
    v = int(v)
    return '<span class="asg-zero">—</span>' if v == 0 else f'<span class="{cls}">{v:,}</span>'


# Project layout (the SLA-by-severity summary): per severity SLA Met, SLA not Met and
# SLA Met %, then one Overall SLA % per row. SLA not Met = resolved late + open breached.
PROJECT_COLS = ["SLA Met", "SLA not Met", "SLA Met %"]


def project_summary_frame(table):
    """The project layout as a flat table for Excel: Project, then per severity
    "<sev> SLA Met", "<sev> SLA not Met", "<sev> SLA Met %", then "Overall SLA %"."""
    rows = []
    for _, r in table.iterrows():
        row = {"Project": r["Team"]}
        for sev in config.SLA_MINUTES:
            pct = r[f"{sev} SLA %"]
            row[f"{sev} SLA Met"] = int(r[f"{sev} Within SLA"])
            row[f"{sev} SLA not Met"] = metric_value(r, sev, "Breached SLA")
            row[f"{sev} SLA Met %"] = None if pct is None or pd.isna(pct) else float(pct)
        pct_all = overall_pct(r)
        row["Overall SLA %"] = pct_all
        rows.append(row)
    return pd.DataFrame(rows)


def project_html(table, wrap_breached=None):
    """The project SLA table as HTML (needs CSS above on the page).
    wrap_breached(team, sev, count_html, count) may wrap a non-zero SLA not Met
    count (e.g. in a ticket popup); its output replaces the count's HTML. `team`
    is the row's label (a project name, or Total)."""
    sevs = list(config.SLA_MINUTES.items())
    head1 = '<th rowspan="2">Project</th>' + "".join(
        f'<th colspan="{len(PROJECT_COLS)}" style="text-align:center;">{sev} &middot; SLA {mins // 60} h</th>'
        for sev, mins in sevs) + '<th rowspan="2">Overall SLA %</th>'
    head2 = "".join(f'<th>{m}</th>' for _ in sevs for m in PROJECT_COLS)

    def cell_html(r, sev, m):
        if m == "SLA Met %":
            return sla_cell(r[f"{sev} SLA %"])
        if m == "SLA Met":
            return count_cell(metric_value(r, sev, "Within SLA"), METRIC_CLASS["Within SLA"])
        value = metric_value(r, sev, "Breached SLA")
        inner = count_cell(value, METRIC_CLASS["Breached SLA"])
        if wrap_breached and value:
            inner = wrap_breached(r["Team"], sev, inner, value)
        return inner

    body = []
    for _, r in table.iterrows():
        is_total = r["Team"] == config.TOTAL_LABEL
        cells = "".join(f'<td>{cell_html(r, sev, m)}</td>' for sev, _mins in sevs for m in PROJECT_COLS)
        cells += f'<td>{sla_cell(overall_pct(r))}</td>'
        cls = ' class="asg-grand"' if is_total else ""
        body.append(f'<tr{cls}><td><span class="asg-name">{html.escape(str(r["Team"]))}</span></td>{cells}</tr>')
    wrap_cls = "asg-wrap asg-popups" if wrap_breached else "asg-wrap"
    return (f'<div class="{wrap_cls}"><table class="asg-table"><thead><tr>{head1}</tr><tr>{head2}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def project_png(table, title, subtitle):
    """The project SLA table as a PNG (Pillow), same layout as project_html."""

    def font(size, bold=False):
        try:
            return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
        except OSError:
            return ImageFont.load_default(size=size)

    def center(text, x0, y0, x1, y1, f, fill):
        box = d.textbbox((0, 0), text, font=f)
        tw, th = box[2] - box[0], box[3] - box[1]
        d.text((x0 + (x1 - x0 - tw) / 2 - box[0], y0 + (y1 - y0 - th) / 2 - box[1]), text, font=f, fill=fill)

    sevs = list(config.SLA_MINUTES.items())
    pad, name_w, num_w, overall_w = 24, 300, 130, 170
    head1_h, head2_h, row_h = 44, 36, 48
    group_w = num_w * len(PROJECT_COLS)
    width = pad * 2 + name_w + group_w * len(sevs) + overall_w
    top = pad + 80
    height = top + head1_h + head2_h + row_h * len(table) + pad
    img = Image.new("RGB", (width, height), "#ffffff")
    d = ImageDraw.Draw(img)
    dark, grey, rule, band = "#20202c", "#5b5b66", "#d9d9e0", "#f4f4f8"
    metric_color = {"SLA Met": COLOR_GOOD, "SLA not Met": COLOR_CRITICAL}

    d.text((pad, pad), title, font=font(26, True), fill=dark)
    d.text((pad, pad + 38), subtitle, font=font(15), fill=grey)

    d.rectangle([pad, top, width - pad, top + head1_h + head2_h], fill=dark)
    center("Project", pad, top, pad + name_w, top + head1_h + head2_h, font(16, True), "#ffffff")
    for i, (sev, mins) in enumerate(sevs):
        x0 = pad + name_w + i * group_w
        center(f"{sev}  ·  SLA {mins // 60} h", x0, top, x0 + group_w, top + head1_h, font(16, True), "#ffffff")
        d.line([x0, top, x0, top + head1_h + head2_h], fill="#55556a", width=2)
        for j, lab in enumerate(PROJECT_COLS):
            cx = x0 + j * num_w
            center(lab, cx, top + head1_h, cx + num_w, top + head1_h + head2_h, font(14, True), "#c9c9d6")

    x_overall = pad + name_w + len(sevs) * group_w
    d.line([x_overall, top, x_overall, top + head1_h + head2_h], fill="#55556a", width=2)
    center("Overall SLA %", x_overall, top, x_overall + overall_w, top + head1_h + head2_h, font(16, True), "#ffffff")

    y = top + head1_h + head2_h
    for _, r in table.iterrows():
        is_total = r["Team"] == config.TOTAL_LABEL
        if is_total:
            d.rectangle([pad, y, width - pad, y + row_h], fill=band)
        d.line([pad, y + row_h, width - pad, y + row_h], fill=rule, width=1)
        center(str(r["Team"]), pad + 14, y, pad + name_w, y + row_h, font(16, True) if is_total else font(16), dark)
        for i, (sev, _mins) in enumerate(sevs):
            x0 = pad + name_w + i * group_w
            for j, m in enumerate(PROJECT_COLS):
                cx = x0 + j * num_w
                if m == "SLA Met %":
                    pct = r[f"{sev} SLA %"]
                    has_pct = not (pct is None or pd.isna(pct))
                    center(f"{pct:.1f}%" if has_pct else "—", cx, y, cx + num_w, y + row_h, font(16, True),
                           pct_color(pct if has_pct else None))
                else:
                    v = metric_value(r, sev, "Within SLA" if m == "SLA Met" else "Breached SLA")
                    center("—" if v == 0 else f"{v:,}", cx, y, cx + num_w, y + row_h, font(16, is_total),
                           metric_color[m])
        pct_all = overall_pct(r)
        center(f"{pct_all:.1f}%" if pct_all is not None else "—", x_overall, y, x_overall + overall_w, y + row_h,
               font(16, True), pct_color(pct_all))
        y += row_h

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
