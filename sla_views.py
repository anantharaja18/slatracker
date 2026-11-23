"""
Shared SLA-by-severity view: the HTML table, the PNG image and the styles used
by the dashboard and by the Resolved Tickets History page.

Input is a DataFrame with one row per team (plus "All teams" last). For each
severity in config.SLA_MINUTES (e.g. Sev-1, Sev-2) it has these columns:
    "<sev> Resolved", "<sev> Within SLA", "<sev> Resolved late", "<sev> SLA %"
and, optionally, "<sev> Open breached" (the dashboard shows it; the history
page does not, because it only looks at resolved tickets). The table shows
Resolved, Within SLA, Breached SLA (= resolved late + open breached) and SLA %.
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


def metrics_of(table=None):
    """The metrics shown under each severity, in order."""
    return ["Resolved", "Within SLA", "Breached SLA", "SLA %"]


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


def overall_total(r):
    """Total Resolved across all severities = Within SLA + Breached SLA for each severity."""
    return sum(metric_value(r, sev, "Resolved") for sev in config.SLA_MINUTES)


def overall_pct(r):
    """SLA % across all severities for one team row: within / (within + breached)."""
    within = sum(int(r[f"{sev} Within SLA"]) for sev in config.SLA_MINUTES)
    breached = sum(metric_value(r, sev, "Breached SLA") for sev in config.SLA_MINUTES)
    judged = within + breached
    return round(within / judged * 100, 1) if judged else None


def count_cell(v, cls=""):
    v = int(v)
    return '<span class="asg-zero">—</span>' if v == 0 else f'<span class="{cls}">{v:,}</span>'


def sla_severity_html(table, wrap_breached=None):
    """The per-team, per-severity SLA table as HTML (needs CSS above on the page).
    wrap_breached(team, sev, count_html, count) may wrap a non-zero Breached SLA
    count (e.g. in a ticket popup); its output replaces the count's HTML."""
    sevs = list(config.SLA_MINUTES.items())
    metrics = metrics_of(table)
    head = '<th>Team</th>' + "".join(
        f'<th colspan="{len(metrics)}" style="text-align:center;">{sev} &middot; SLA {mins // 60} h</th>'
        for sev, mins in sevs)
    subhead = '<th></th>' + "".join(f'<th>{m}</th>' for _ in sevs for m in metrics) + '<th>Total Resolved</th><th>SLA %</th>'
    head += '<th colspan="2" style="text-align:center;">Overall</th>'

    def cell_html(r, sev, m):
        if m == "SLA %":
            return sla_cell(r[f"{sev} SLA %"])
        value = metric_value(r, sev, m)
        inner = count_cell(value, METRIC_CLASS.get(m, ""))
        if m == "Breached SLA" and wrap_breached and value:
            inner = wrap_breached(r["Team"], sev, inner, value)
        return inner

    body = []
    for _, r in table.iterrows():
        is_all = r["Team"] == "All teams"
        dot = "" if is_all else f'<span class="dot" style="background:{TEAM_COLOR.get(r["Team"], TEXT_MUTED)};"></span>'
        cells = "".join(f'<td>{cell_html(r, sev, m)}</td>' for sev, _mins in sevs for m in metrics)
        cells += f'<td>{count_cell(overall_total(r))}</td><td>{sla_cell(overall_pct(r))}</td>'
        cls = ' class="asg-grand"' if is_all else ""
        body.append(f'<tr{cls}><td><span class="asg-name">{dot}{html.escape(str(r["Team"]))}</span></td>{cells}</tr>')
    wrap_cls = "asg-wrap asg-popups" if wrap_breached else "asg-wrap"
    return (f'<div class="{wrap_cls}"><table class="asg-table"><thead><tr>{head}</tr><tr>{subhead}</tr></thead>'
            f'<tbody>{"".join(body)}</tbody></table></div>')


def severity_png(table, title, subtitle):
    """The same table as a PNG (Pillow)."""

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
    metrics = metrics_of(table)
    pad, team_w, num_w = 24, 240, 118
    head1_h, head2_h, row_h = 44, 36, 44
    group_w = num_w * len(metrics)
    width = pad * 2 + team_w + group_w * len(sevs) + num_w * 2
    top = pad + 80
    height = top + head1_h + head2_h + row_h * len(table) + pad
    img = Image.new("RGB", (width, height), "#ffffff")
    d = ImageDraw.Draw(img)
    dark, grey, rule, band = "#20202c", "#5b5b66", "#d9d9e0", "#f4f4f8"
    metric_color = {"Within SLA": COLOR_GOOD, "Breached SLA": COLOR_CRITICAL}

    d.text((pad, pad), title, font=font(26, True), fill=dark)
    d.text((pad, pad + 38), subtitle, font=font(15), fill=grey)

    d.rectangle([pad, top, width - pad, top + head1_h + head2_h], fill=dark)
    center("Team", pad, top, pad + team_w, top + head1_h + head2_h, font(16, True), "#ffffff")
    for i, (sev, mins) in enumerate(sevs):
        x0 = pad + team_w + i * group_w
        center(f"{sev}  ·  SLA {mins // 60} h", x0, top, x0 + group_w, top + head1_h, font(16, True), "#ffffff")
        d.line([x0, top, x0, top + head1_h + head2_h], fill="#55556a", width=2)
        for j, lab in enumerate(metrics):
            cx = x0 + j * num_w
            center(lab, cx, top + head1_h, cx + num_w, top + head1_h + head2_h, font(14, True), "#c9c9d6")

    x_overall = pad + team_w + len(sevs) * group_w
    center("Overall", x_overall, top, x_overall + num_w * 2, top + head1_h, font(16, True), "#ffffff")
    center("Total Resolved", x_overall, top + head1_h, x_overall + num_w, top + head1_h + head2_h, font(14, True), "#c9c9d6")
    center("SLA %", x_overall + num_w, top + head1_h, x_overall + num_w * 2, top + head1_h + head2_h, font(14, True), "#c9c9d6")

    y = top + head1_h + head2_h
    for _, r in table.iterrows():
        is_all = r["Team"] == "All teams"
        if is_all:
            d.rectangle([pad, y, width - pad, y + row_h], fill=band)
        d.line([pad, y + row_h, width - pad, y + row_h], fill=rule, width=1)
        cy = y + row_h / 2
        name_x = pad + 14
        if not is_all:
            d.ellipse([pad + 14, cy - 6, pad + 26, cy + 6], fill=TEAM_COLOR.get(r["Team"], grey))
            name_x = pad + 34
        center(str(r["Team"]), name_x, y, pad + team_w, y + row_h, font(16, True) if is_all else font(16), dark)
        for i, (sev, _mins) in enumerate(sevs):
            x0 = pad + team_w + i * group_w
            for j, m in enumerate(metrics):
                cx = x0 + j * num_w
                if m == "SLA %":
                    pct = r[f"{sev} SLA %"]
                    has_pct = not (pct is None or pd.isna(pct))
                    center(f"{pct:.1f}%" if has_pct else "—", cx, y, cx + num_w, y + row_h, font(16, True),
                           pct_color(pct if has_pct else None))
                else:
                    v = metric_value(r, sev, m)
                    center("—" if v == 0 else f"{v:,}", cx, y, cx + num_w, y + row_h, font(16, is_all),
                           metric_color.get(m, dark))
        pct_all = overall_pct(r)
        total_all = overall_total(r)
        center("—" if total_all == 0 else f"{total_all:,}", x_overall, y, x_overall + num_w, y + row_h,
               font(16, is_all), dark)
        center(f"{pct_all:.1f}%" if pct_all is not None else "—", x_overall + num_w, y, x_overall + num_w * 2, y + row_h,
               font(16, True), pct_color(pct_all))
        y += row_h

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()
