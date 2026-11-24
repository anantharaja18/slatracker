"""
Productivity email: the Historical page's "Resolved by assignee" table (all four
teams, all resolved tickets) as a picture in the body, with the Excel file attached.

    Hourly Productivity Report   same frequency and hours as the SLA email
                                 (08:00-23:00 Puerto Rico time), today so far.
    Productivity Report - EOD    00:00:00 Jira time, covering the previous full day.

The numbers come from the same local cache as the Historical page (trend_store's
`resolved` table, synced from Jira before each send), and the same by_assignee rules.
"""

import html
import io
from datetime import datetime

import pandas as pd
from PIL import Image, ImageDraw, ImageFont

import breach_logic
import config
import resolved_analysis as ra
import sla_report
import smtp_mail
import trend_store

HOURLY_KIND, EOD_KIND = "productivity_hourly", "productivity_eod"


def _build(day_start, day_end):
    """(table, resolved) for the period. Brings the cache up to date first; a busy sync is skipped."""
    trend_store.sync()
    resolved = ra.load_resolved()
    if resolved.empty:
        return pd.DataFrame(), resolved
    return ra.by_assignee(resolved, day_start, day_end, ra.TEAMS), resolved


def _font(size, bold=False):
    try:
        return ImageFont.truetype("arialbd.ttf" if bold else "arial.ttf", size)
    except OSError:
        return ImageFont.load_default(size=size)


def assignee_png(table, title, subtitle):
    """The Resolved-by-assignee table as a PNG (Pillow), same columns as the Historical page."""
    import sla_views  # imports Streamlit helpers, so loaded only when a picture is drawn
    from ui_common import COLOR_CRITICAL, COLOR_GOOD

    def center(text, x0, y0, x1, y1, f, fill):
        d_box = d.textbbox((0, 0), text, font=f)
        tw, th = d_box[2] - d_box[0], d_box[3] - d_box[1]
        d.text((x0 + (x1 - x0 - tw) / 2 - d_box[0], y0 + (y1 - y0 - th) / 2 - d_box[1]), text, font=f, fill=fill)

    teams = [c[: -len(" (Resolved)")] for c in table.columns if c.endswith(" (Resolved)")]
    cols = [("Assignee", 300, None)]
    cols += [(t, 120, f"{t} (Resolved)") for t in teams]
    cols += [("Total resolved", 140, "Total Resolved"),
             ("Resolved from\ntoday's inflow", 180, ra.INFLOW_SAME_DAY),
             ("Resolved from\npast inflow", 180, ra.INFLOW_PAST),
             ("SLA Met", 120, "SLA Met"),
             ("SLA not Met", 130, "SLA not Met"),
             ("SLA %", 150, "SLA %")]

    pad, head_h, row_h = 24, 60, 40
    width = pad * 2 + sum(w for _, w, _ in cols)
    top = pad + 80
    records = table.to_dict("records")
    height = top + head_h + row_h * len(records) + pad + 40
    img = Image.new("RGB", (width, height), "#ffffff")
    d = ImageDraw.Draw(img)
    dark, grey, rule, band = "#20202c", "#5b5b66", "#d9d9e0", "#f4f4f8"
    metric_color = {"SLA Met": COLOR_GOOD, "SLA not Met": COLOR_CRITICAL}

    d.text((pad, pad), title, font=_font(26, True), fill=dark)
    d.text((pad, pad + 38), subtitle, font=_font(15), fill=grey)

    d.rectangle([pad, top, width - pad, top + head_h], fill=dark)
    xs, x = [], pad
    for heading, w, _key in cols:
        xs.append((x, x + w))
        lines = heading.split("\n")
        if len(lines) == 1:
            center(heading, x, top, x + w, top + head_h, _font(15, True), "#ffffff")
        else:
            for i, line in enumerate(lines):
                center(line, x, top + i * head_h // 2, x + w, top + (i + 1) * head_h // 2, _font(13, True), "#ffffff")
        x += w
    d.line([xs[0][1] - 1, top, xs[0][1] - 1, top + head_h], fill="#55556a", width=1)

    y = top + head_h
    for r in records:
        is_total = r["Assignee"] == "Grand Total"
        if is_total:
            d.rectangle([pad, y, width - pad, y + row_h], fill=band)
        d.line([pad, y + row_h, width - pad, y + row_h], fill=rule, width=1)
        for (heading, _w, key), (x0, x1) in zip(cols, xs):
            if key is None:  # the name column is left-aligned, as on the dashboard
                name_font = _font(16, True)
                box = d.textbbox((0, 0), str(r["Assignee"]), font=name_font)
                d.text((x0 + 14 - box[0], y + (row_h - (box[3] - box[1])) / 2 - box[1]),
                       str(r["Assignee"]), font=name_font, fill=dark)
                continue
            value = r[key]
            if key == "SLA %":
                has = not pd.isna(value)
                center(f"{value:.1f}%" if has else "—", x0, y, x1, y + row_h, _font(16, True),
                       sla_views.pct_color(value if has else None))
                continue
            n = int(value)
            center("—" if n == 0 else f"{n:,}", x0, y, x1, y + row_h, _font(16, is_total),
                   metric_color.get(key, dark))
        y += row_h

    legend = ("SLA %: 90% and above · 70 – 89% · below 70% · "
              "SLA % = SLA Met ÷ (SLA Met + SLA not Met)")
    d.text((pad, y + 12), legend, font=_font(13), fill=grey)

    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def assignee_excel(table, resolved, day_start, day_end, period_label):
    """Excel: By Assignee (the table as in the picture), Tickets (every resolved ticket
    behind it, with Jira links) and Filters."""
    import ui_common
    if resolved.empty:
        tickets = pd.DataFrame()
    else:
        tickets = ra.ticket_list(resolved, day_start, day_end)
        tickets = tickets.assign(Link=tickets["Key"].map(lambda k: f"{config.JIRA_BASE_URL.rstrip('/')}/browse/{k}"))
    info = pd.DataFrame([{"Period": period_label, "Timezone": breach_logic.jira_time_label(),
                          "Teams": ", ".join(ra.TEAMS), "Resolved tickets": ra.INFLOW_ALL,
                          "Assignees": "All"}])
    return ui_common.write_excel({"By Assignee": table, "Tickets": tickets, "Filters": info})


def _body(title, as_of, table, has_picture):
    if has_picture:
        picture = f'<img src="cid:{smtp_mail.INLINE_PNG_CID}" alt="Resolved by assignee" style="max-width:100%;">'
    else:
        picture = "<p>No resolved tickets for these teams in this period.</p>"
    return (f"<p><b>{html.escape(title)}</b><br>{html.escape(as_of)}</p>{picture}"
            "<p>The full ticket list is in the attached Excel file.</p>")


def _send(subject, to, title, as_of, day_start, day_end, period_label, file_name):
    table, resolved = _build(day_start, day_end)
    png = assignee_png(table, title, as_of) if not table.empty else None
    xlsx = assignee_excel(table, resolved, day_start, day_end, period_label)
    body = _body(title, as_of, table, png is not None)
    smtp_mail.send(subject, body, to, png=png, attachment=(file_name, xlsx))


def _recipients(to):
    """`to` as given (the test list may be empty, and must never fall back to live recipients)."""
    return smtp_mail.live_recipients() if to is None else to


def _logged(kind, to, action):
    """Live sends are logged for the admin panel; test sends (to=...) are not."""
    try:
        action()
    except Exception as exc:
        if to is None:
            sla_report._log_email(kind, False, str(exc))
        raise
    if to is None:
        sla_report._log_email(kind, True, f"sent to {len(smtp_mail.live_recipients())} recipient(s)")


def send_hourly(to=None):
    """Hourly Productivity Report: today so far. `to` defaults to the live recipients."""
    def action():
        now_pr = datetime.now(sla_report.REPORT_TZ)
        jira_now = breach_logic.jira_now()
        today = jira_now.date()
        title = f"Resolved by Assignee — Today's Inflow — {jira_now:%d %b %Y}"
        as_of = f"As of {now_pr:%H:%M} Puerto Rico time ({jira_now:%H:%M %Z} Jira time)"
        _send(sla_report.email_subject("Hourly Productivity Report", today, f"as of {now_pr:%H:%M} PR time"),
              _recipients(to), title, as_of, today, today,
              f"{today:%d %b %Y} (today so far)",
              f"Productivity_By_Assignee_{today:%Y-%m-%d}_{now_pr:%H%M}.xlsx")
    _logged(HOURLY_KIND, to, action)


def send_eod(day, to=None):
    """Productivity Report - EOD for `day` (the previous full day)."""
    def action():
        title = f"Resolved by Assignee — Resolved on {day:%d %b %Y}"
        as_of = f"Full day {day:%d %b %Y} (Jira time)"
        _send(sla_report.email_subject("Daily Productivity Report", day),
              _recipients(to), title, as_of, day, day,
              f"{day:%d %b %Y} (full day)", f"Productivity_By_Assignee_{day:%Y-%m-%d}.xlsx")
    _logged(EOD_KIND, to, action)


def send_test():
    """Hourly Productivity Report to SMTP_TEST_TO only."""
    send_hourly(to=smtp_mail.test_recipients())
