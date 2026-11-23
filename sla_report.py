"""
Posts the dashboard's "SLA by Severity -- Today's Inflow" table to the Teams
channel (config.TEAMS_CHANNEL_WEBHOOK_URL) every hour from 08:00 to 22:00
Puerto Rico time (America/Puerto_Rico, UTC-4).

    python sla_report.py              keep running; posts at each slot
    python sla_report.py --now        post one report right away
    python sla_report.py --dry-run    print the message, post nothing

The numbers follow the dashboard's rules: today's inflow resolved tickets
(within SLA / resolved late) plus open tickets that breached today. SLA % =
within / (within + resolved late + open breached). Keep these rules in step
with dashboard.py if they ever change.
"""

import argparse
import base64
import html
import os
import socket
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import requests

import breach_logic
import config
import jira_client
import report_settings
import sla_comments
import smtp_mail

REPORT_TZ = ZoneInfo("America/Puerto_Rico")
BUCKET_BREACHED_TODAY = "Breached Today"  # same meaning as in dashboard.py

RESOLVED_FIELDS = ["summary", "status", "assignee", "created", "resolutiondate", config.SEVERITY_FIELD,
                   breach_logic.JIRA_SLA_FIELD]
OPEN_FIELDS = ["summary", "status", "assignee", "created", config.SEVERITY_FIELD, breach_logic.JIRA_SLA_FIELD,
               breach_logic.ESCALATION_PATH_FIELD]


def fetch_resolved_today():
    """Resolved tickets per team, with whether each was created today."""
    today = breach_logic.jira_now().date()
    frames = []
    for team, cat_config in config.CATEGORIES.items():
        rows = []
        for issue in jira_client.search_issues(cat_config["resolved_jql"], fields=RESOLVED_FIELDS):
            result = breach_logic.evaluate_resolution(issue, cat_config)
            created = breach_logic._parse_datetime(issue["fields"].get("created"))
            rows.append({
                "Team": team,
                "Key": issue["key"],
                "Severity": result["severity"] or "N/A",
                "Resolved Status": result["status"],
                "Created Today": created is not None and created.date() == today,
            })
        frames.append(pd.DataFrame(rows, columns=["Team", "Key", "Severity", "Resolved Status", "Created Today"]))
    all_rows = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(
        columns=["Team", "Key", "Severity", "Resolved Status", "Created Today"])
    return all_rows[all_rows["Created Today"]]


def fetch_open_breached_today():
    """Open tickets whose SLA ran out today, per team (a team's backlog query
    is included, as on the dashboard)."""
    today = breach_logic.jira_now().date()
    rows = []
    for team, cat_config in config.CATEGORIES.items():
        jqls = [cat_config["jql"]]
        if cat_config.get("backlog_jql"):
            jqls.append(cat_config["backlog_jql"])
        seen = set()
        for jql in jqls:
            for issue in jira_client.search_issues(jql, fields=OPEN_FIELDS):
                if issue["key"] in seen:
                    continue
                seen.add(issue["key"])
                result = breach_logic.evaluate(issue, cat_config)
                hours, due = result["hours_remaining"], result["due_date"]
                if hours is None or pd.isna(hours) or due is None:
                    continue
                path = issue["fields"].get(breach_logic.ESCALATION_PATH_FIELD)
                path = path.get("value") if isinstance(path, dict) else path
                if breach_logic.is_excluded_open_breached(
                        (issue["fields"].get("status") or {}).get("name"), path):
                    continue  # Escalated on Network / A2P: not counted
                if hours * 60 < 0 and due.date() >= today:
                    f = issue["fields"]
                    rows.append({"Team": team, "Key": issue["key"], "Severity": result["severity"] or "N/A",
                                 "Summary": f.get("summary") or "", "Status": (f.get("status") or {}).get("name", ""),
                                 "Assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
                                 "Created": created_text(f.get("created")),
                                 "Due": result["due_date"].strftime("%Y-%m-%d %H:%M") if result["due_date"] else "",
                                 "Escalation Path": escalation_text(f)})
    return pd.DataFrame(rows, columns=["Team", "Key", "Severity", "Summary", "Status", "Assignee", "Created", "Due",
                                       "Escalation Path"])


def summary_rows(resolved, open_breached):
    """One row per team and severity (plus All teams), each ticket counted once
    in the All teams rows."""
    rows = []
    for team in [*config.CATEGORIES, "All teams"]:
        res = resolved.drop_duplicates("Key") if team == "All teams" else resolved[resolved["Team"] == team]
        opn = open_breached.drop_duplicates("Key") if team == "All teams" else open_breached[open_breached["Team"] == team]
        for sev in config.SLA_MINUTES:
            res_sev = res[res["Severity"] == sev]
            within = int((res_sev["Resolved Status"] == breach_logic.STATUS_OK).sum())
            late = int((res_sev["Resolved Status"] == breach_logic.STATUS_BREACHED).sum())
            open_n = int((opn["Severity"] == sev).sum())
            judged = within + late + open_n
            rows.append({
                "Team": team, "Sev": sev, "Resolved": len(res_sev), "Within": within,
                "Late": late, "Open": open_n,
                "SLA": round(within / judged * 100, 1) if judged else None,
            })
    return rows


def breached_count(r):
    """Breached SLA = resolved late + open breached. End-of-day rows have no open count ("—")."""
    return r["Late"] + (r["Open"] if isinstance(r["Open"], int) else 0)


def format_table(rows):
    """Fixed-width table for a Teams code block."""
    head = f"{'Team':<14}{'Sev':<7}{'Total Resolved':>15}{'Within SLA':>12}{'Breached SLA':>14}{'SLA %':>8}"
    lines = [head, "-" * len(head)]
    for i, r in enumerate(rows):
        if r["Team"] == "All teams" and rows[i - 1]["Team"] != "All teams":
            lines.append("-" * len(head))
        pct = "—" if r["SLA"] is None else f"{r['SLA']:.1f}%"
        lines.append(f"{r['Team']:<14}{r['Sev']:<7}{r['Resolved']:>15}{r['Within']:>12}"
                     f"{breached_count(r):>14}{pct:>8}")
    return "\n".join(lines)


def build_parts():
    """The report's pieces: title, as-of line, table and footnote."""
    now_pr = datetime.now(REPORT_TZ)
    jira_now = breach_logic.jira_now()
    rows = summary_rows(fetch_resolved_today(), fetch_open_breached_today())
    sev_text = ", ".join(f"{s} {m // 60} h" for s, m in config.SLA_MINUTES.items())
    return {
        "title": f"SLA by Severity — Today's Inflow — {jira_now:%d %b %Y}",
        "as_of": f"As of {now_pr:%H:%M} Puerto Rico time ({jira_now:%H:%M %Z} Jira time)",
        "table": format_table(rows),
        "rows": rows,
        "footnote": (f"Within SLA = resolved before Created + the severity's SLA ({sev_text}). "
                     "Breached SLA = resolved late + open tickets that breached their SLA today. "
                     "SLA % = within ÷ (within + breached)."),
    }


def build_card(parts):
    """Channel webhook message (MessageCard, markdown)."""
    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "0076D7",
        "summary": parts["title"],
        "title": parts["title"],
        "text": parts["as_of"] + "\n\n```\n" + parts["table"] + "\n```\n" + parts["footnote"],
    }


def report_html(parts):
    """The same report as HTML, for a Teams chat message (Power Automate)."""
    return (f"<b>{html.escape(parts['title'])}</b><br>{html.escape(parts['as_of'])}<br>"
            f"<pre>{html.escape(parts['table'])}</pre>{html.escape(parts['footnote'])}")


def workflow_payload(parts):
    """Adaptive Card message for a Teams Workflows webhook (post to a chat).
    The table uses the Adaptive Cards 1.5 Table element, which draws grid lines."""

    def cell(text, column, bold=False, emphasis=False, color=None):
        text_block = {
            "type": "TextBlock", "text": str(text), "size": "Small", "wrap": False,
            "weight": "Bolder" if bold else "Default",
            "horizontalAlignment": "Left" if column < 2 else "Right",
        }
        if color:
            text_block["color"] = color
        return {"type": "TableCell", "items": [text_block], "style": "emphasis" if emphasis else "default"}

    header = ["Team", "Sev", "Total Resolved", "Within SLA", "Breached SLA", "SLA %"]
    table_rows = [{"type": "TableRow", "style": "accent",
                   "cells": [cell(v, i, bold=True) for i, v in enumerate(header)]}]
    for r in parts["rows"]:
        # Order Fallout only has Sev-2 tickets, so leave out its empty Sev-1 row.
        if r["Team"] == "Order Fallout" and r["Sev"] == "Sev-1" and not (r["Resolved"] or r["Within"] or r["Late"]):
            continue
        is_all = r["Team"] == "All teams"
        pct = "—" if r["SLA"] is None else f"{r['SLA']:.1f}%"
        # Same thresholds as the dashboard: 90%+ green, 70-89% amber, below 70% red.
        color = None
        if r["SLA"] is not None:
            color = "Good" if r["SLA"] >= 90 else "Warning" if r["SLA"] >= 70 else "Attention"
        values = [r["Team"], r["Sev"], r["Resolved"], r["Within"], breached_count(r), pct]
        cells = [cell(v, i, bold=is_all, emphasis=is_all, color=color if i == 5 else None)
                 for i, v in enumerate(values)]
        table_rows.append({"type": "TableRow", "cells": cells})

    table = {
        "type": "Table",
        "gridStyle": "accent",
        "showGridLines": True,
        "firstRowAsHeader": True,
        "verticalCellContentAlignment": "center",
        # Widths are shares of the card width (wide enough that no label is cut off):
        # Team 9, Sev 6, Total Resolved 12, Within SLA 11, Breached SLA 11, SLA % 8.
        "columns": [{"width": 9}, {"width": 6}, {"width": 12}, {"width": 11}, {"width": 11}, {"width": 8}],
        "rows": table_rows,
    }
    card_body = [
        {"type": "TextBlock", "text": parts["title"], "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": parts["as_of"], "isSubtle": True, "wrap": True},
        table,
        {"type": "TextBlock", "text": parts["footnote"], "size": "Small", "isSubtle": True,
         "wrap": True, "spacing": "Medium"},
    ]
    return {
        "type": "message",
        "attachments": [{
            "contentType": "application/vnd.microsoft.card.adaptive",
            "contentUrl": None,
            "content": {
                "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                "type": "AdaptiveCard",
                "version": "1.5",
                "body": card_body,
            },
        }],
    }


def post_report(dry_run=False):
    """Build the report and post it to the saved Workflow link. Returns True if posted."""
    parts = build_parts()
    if dry_run:
        print(parts["title"])
        print(parts["as_of"])
        print(parts["table"])
        print(parts["footnote"])
        return True
    url = report_settings.webhook_url()
    if not url:
        raise RuntimeError("No Teams workflow link is saved yet")
    resp = requests.post(url, json=workflow_payload(parts), timeout=30)
    resp.raise_for_status()
    print(f"[sla_report] Posted to chat at {datetime.now(REPORT_TZ):%Y-%m-%d %H:%M %Z}")
    return True


def slot_hours(interval):
    """Start hours of the 08:00-22:00 window every `interval` hours, e.g. 1 -> 8..22, 3 -> 8, 11, 14, 17, 20."""
    return list(range(8, 23, interval))


def next_slot(now, interval=1):
    """The next slot after `now` in the 08:00-22:00 window, every `interval` hours, Puerto Rico time."""
    hours = slot_hours(interval)
    for hour in hours:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=hours[0], minute=0, second=0, microsecond=0)


EOD_FIELDS = ["summary", "status", "assignee", "created", "resolutiondate", config.SEVERITY_FIELD,
              breach_logic.JIRA_SLA_FIELD, breach_logic.ESCALATION_PATH_FIELD]


def escalation_text(fields):
    """Escalation path as plain text ("" if none)."""
    value = fields.get(breach_logic.ESCALATION_PATH_FIELD)
    value = value.get("value") if isinstance(value, dict) else value
    return value or ""


def created_text(raw):
    """Created timestamp as "YYYY-MM-DD HH:MM" in Jira time ("" if missing)."""
    parsed = breach_logic._parse_datetime(raw)
    return parsed.astimezone(breach_logic.JIRA_TZ).strftime("%Y-%m-%d %H:%M") if parsed else ""
REPORTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "reports")


def fetch_resolved_on_day(day):
    """Every ticket resolved on `day` (00:00:00 to 23:59:59 Jira time) in each
    team's scope, whenever it was created."""
    start = f"{day:%Y/%m/%d} 00:00"
    end = f"{day + timedelta(days=1):%Y/%m/%d} 00:00"
    rows = []
    for team, cat_config in config.CATEGORIES.items():
        jql = f'{cat_config["resolved_scope_jql"]} AND resolutiondate >= "{start}" AND resolutiondate < "{end}"'
        for issue in jira_client.search_issues(jql, fields=EOD_FIELDS):
            f = issue["fields"]
            result = breach_logic.evaluate_resolution(issue, cat_config)
            created = breach_logic._parse_datetime(f.get("created"))
            resolved = breach_logic._parse_datetime(f.get("resolutiondate"))
            created_on_day = created is not None and created.astimezone(breach_logic.JIRA_TZ).date() == day
            rows.append({
                "Team": team, "Key": issue["key"], "Summary": f.get("summary", ""),
                "Severity": result["severity"] or "N/A",
                "Status": (f.get("status") or {}).get("name", ""),
                "Assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
                "Created": created, "Resolved": resolved,
                "Resolved Status": result["status"],
                "Created Today": created_on_day,
                "Escalation Path": escalation_text(f),
            })
    return pd.DataFrame(rows, columns=["Team", "Key", "Summary", "Severity", "Status", "Assignee",
                                       "Created", "Resolved", "Resolved Status", "Created Today", "Escalation Path"])


def eod_ticket_sheet(resolved_day):
    """Every ticket resolved on the day, one row each, with Jira links."""
    labels = {breach_logic.STATUS_OK: "Within SLA", breach_logic.STATUS_BREACHED: "Resolved late"}

    def stamp(value):
        return "" if value is None or pd.isna(value) else value.astimezone(breach_logic.JIRA_TZ).strftime("%Y-%m-%d %H:%M")

    sheet = pd.DataFrame({
        "Team": resolved_day["Team"].values,
        "Key": resolved_day["Key"].values,
        "Summary": resolved_day["Summary"].values,
        "Severity": resolved_day["Severity"].values,
        "Status": resolved_day["Status"].values,
        "Assignee": resolved_day["Assignee"].values,
        "Created": resolved_day["Created"].map(stamp).values,
        "Resolved": resolved_day["Resolved"].map(stamp).values,
        "Created & Resolved Same Day": resolved_day["Created Today"].map({True: "Yes", False: "No"}).values,
        "SLA": resolved_day["Resolved Status"].map(labels).fillna("No SLA (severity not set)").values,
        "Escalation Path": resolved_day["Escalation Path"].fillna("").values,
    })
    reasons = breach_reasons()
    sheet["Breached Reason"] = [reasons.get(key, "") if sla == "Resolved late" else ""
                                for key, sla in zip(sheet["Key"], sheet["SLA"])]
    sheet["Link"] = sheet["Key"].map(lambda key: f"{config.JIRA_BASE_URL.rstrip('/')}/browse/{key}")
    return sheet


def previous_day():
    """The last full day in Jira time."""
    return breach_logic.jira_now().date() - timedelta(days=1)


def build_eod_report(day):
    """Builds the end-of-day report for `day`, saves the Excel file, returns the card parts."""
    import ui_common  # Excel styling helper (write_excel)

    resolved_day = fetch_resolved_on_day(day)
    empty_open = pd.DataFrame(columns=["Team", "Key", "Severity"])
    rows = summary_rows(resolved_day, empty_open)  # resolved only: SLA % = within / (within + late)
    for r in rows:
        r["Open"] = "—"
    sheet = eod_ticket_sheet(resolved_day)
    summary = pd.DataFrame(rows)
    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, f"EOD_SLA_Report_{day:%Y-%m-%d}.xlsx")
    with open(path, "wb") as f:
        same_day = sheet["Created & Resolved Same Day"] == "Yes"
        f.write(ui_common.write_excel({
            "Summary": summary,
            "Resolved from Today's Inflow": sheet[same_day],
            "Resolved from Past Inflow": sheet[~same_day],
        }))
    return {
        "title": f"End of day SLA report — {day:%d %b %Y}",
        "as_of": (f"Tickets resolved {day:%d %b %Y} 00:00:00 to 23:59:59 Jira time ({breach_logic.JIRA_TZ}) · "
                  f"{len(resolved_day)} resolved tickets"),
        "rows": rows,
        "footnote": f"Full ticket list ({len(sheet)} tickets, with Jira links) saved to: {path}",
        "path": path,
    }


def ordinal_day(day):
    """'17th Nov 2026' style date."""
    n = day.day
    suffix = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix} {day:%b %Y}"


def email_subject(day, suffix=""):
    """Subject from SMTP_SUBJECT_PREFIX, with "Previous Days Date" replaced by the report date."""
    prefix = os.environ.get("SMTP_SUBJECT_PREFIX", "").strip() or \
        "Daily Ticket Resolution & SLA Report- Previous Days Date"
    text = prefix.replace("Previous Days Date", ordinal_day(day)).replace("Report- ", "Report - ")
    return text + (f" - {suffix}" if suffix else "")


def wide_table(rows):
    """Summary rows (one per team and severity) -> one row per team with a column group per
    severity, the layout of the SLA summary picture."""
    by_team = {}
    for r in rows:
        w = by_team.setdefault(r["Team"], {"Team": r["Team"]})
        sev = r["Sev"]
        open_n = r["Open"] if isinstance(r["Open"], int) else 0
        w.update({f"{sev} Resolved": r["Resolved"], f"{sev} Within SLA": r["Within"],
                  f"{sev} Resolved late": r["Late"], f"{sev} Open breached": open_n, f"{sev} SLA %": r["SLA"]})
    return pd.DataFrame(list(by_team.values()))


def png_summary(rows, title, subtitle):
    import sla_views  # draws the same table as the dashboard's PNG download
    return sla_views.severity_png(wide_table(rows), title, subtitle)


def today_excel(rows, open_breached, day):
    """Excel for today's inflow: Summary, every ticket resolved today from today's inflow,
    and every open ticket breached now (same rules as the dashboard)."""
    import ui_common
    resolved_day = fetch_resolved_on_day(day)
    inflow = eod_ticket_sheet(resolved_day[resolved_day["Created Today"]])
    open_sheet = open_breached.copy()
    reasons = breach_reasons()
    open_sheet["Breached Reason"] = open_sheet["Key"].map(lambda key: reasons.get(key, ""))
    open_sheet["Link"] = open_sheet["Key"].map(lambda key: f"{config.JIRA_BASE_URL.rstrip('/')}/browse/{key}")
    return ui_common.write_excel({
        "Summary": pd.DataFrame(rows),
        "Resolved Today (Inflow)": inflow,
        "Open Breached": open_sheet,
    })


def breach_reasons():
    """Ticket key -> the SLA miss comment written for it (the breach reason)."""
    return {key: note["comment"] for key, note in sla_comments.load_all().items() if note["comment"]}


def _picture_html(png):
    return f'<img src="cid:{smtp_mail.INLINE_PNG_CID}" alt="SLA summary" style="max-width:100%;">' if png else ""


def _log_email(kind, ok, detail):
    """Remembers the last live hourly / end-of-day email for the admin panel."""
    settings = report_settings.load()
    log = dict(settings.get("email_log") or {})
    log[kind] = {"at": datetime.now(REPORT_TZ).strftime("%d %b %H:%M %Z"), "ok": ok, "detail": detail}
    report_settings.save({**settings, "email_log": log})


def send_hourly_email(to=None):
    """Live sends are logged for the admin panel; test sends (to=...) are not."""
    try:
        _send_hourly_email(to)
    except Exception as exc:
        if to is None:
            _log_email("hourly", False, str(exc))
        raise
    if to is None:
        _log_email("hourly", True, f"sent to {len(smtp_mail.live_recipients())} recipient(s)")


def _send_hourly_email(to=None):
    """Hourly email: today's inflow as of now (summary picture in the body, Excel attached).
    `to` defaults to SMTP_TO; the test button passes SMTP_TEST_TO."""
    now_pr = datetime.now(REPORT_TZ)
    jira_now = breach_logic.jira_now()
    today = jira_now.date()
    opn = fetch_open_breached_today()
    rows = summary_rows(fetch_resolved_today(), opn)
    title = f"SLA by Severity — Today's Inflow — {jira_now:%d %b %Y}"
    as_of = f"As of {now_pr:%H:%M} Puerto Rico time ({jira_now:%H:%M %Z} Jira time)"
    png = png_summary(rows, title, as_of)
    xlsx = today_excel(rows, opn, today)
    body = (f"<p><b>{html.escape(title)}</b><br>{html.escape(as_of)}</p>{_picture_html(png)}"
            "<p>The full ticket list is in the attached Excel file.</p>")
    smtp_mail.send(email_subject(today, f"as of {now_pr:%H:%M} PR time"), body,
                   to or smtp_mail.live_recipients(), png=png,
                   attachment=(f"SLA_Today_Inflow_{today:%Y-%m-%d}_{now_pr:%H%M}.xlsx", xlsx))


def send_eod_email(day, parts, to=None):
    """Live sends are logged for the admin panel; test sends (to=...) are not."""
    try:
        _send_eod_email(day, parts, to)
    except Exception as exc:
        if to is None:
            _log_email("eod", False, str(exc))
        raise
    if to is None:
        _log_email("eod", True, f"{day:%d %b %Y}, sent to {len(smtp_mail.live_recipients())} recipient(s)")


def _send_eod_email(day, parts, to=None):
    """End-of-day email for `day`: summary picture and table in the body, Excel attached."""
    with open(parts["path"], "rb") as f:
        xlsx = f.read()
    title = f"SLA by Severity — Resolved on {day:%d %b %Y}"
    png = png_summary(parts["rows"], title, parts["as_of"])
    body = (f"<p><b>{html.escape(title)}</b><br>{html.escape(parts['as_of'])}</p>{_picture_html(png)}"
            "<p>The full ticket list is in the attached Excel file.</p>")
    smtp_mail.send(email_subject(day), body, to or smtp_mail.live_recipients(), png=png,
                   attachment=(os.path.basename(parts["path"]), xlsx))


def send_eod_email_now(day=None):
    """Sends the end-of-day email to the live recipients now (no Teams post)."""
    day = day or previous_day()
    send_eod_email(day, build_eod_report(day))


def send_test_email():
    """Sends the hourly email to SMTP_TEST_TO only."""
    send_hourly_email(to=smtp_mail.test_recipients())


def email_html(parts):
    """The summary table as an HTML email body (no local file paths)."""
    cell_style = "padding:4px 8px;border:1px solid #cccccc;"
    head = "".join(f"<th style='{cell_style}background:#20202c;color:#ffffff;'>{h}</th>"
                   for h in ["Team", "Sev", "Total Resolved", "Within SLA", "Breached SLA", "SLA %"])
    body_rows = []
    for r in parts["rows"]:
        if r["Team"] == "Order Fallout" and r["Sev"] == "Sev-1" and not (r["Resolved"] or r["Within"] or r["Late"]):
            continue
        pct = "—" if r["SLA"] is None else f"{r['SLA']:.1f}%"
        bold = "font-weight:bold;" if r["Team"] == "All teams" else ""
        values = [r["Team"], r["Sev"], r["Resolved"], r["Within"], breached_count(r), pct]
        body_rows.append("<tr>" + "".join(f"<td style='{cell_style}{bold}'>{html.escape(str(v))}</td>"
                                          for v in values) + "</tr>")
    return (f"<p><b>{html.escape(parts['title'])}</b><br>{html.escape(parts['as_of'])}</p>"
            f"<table style='border-collapse:collapse;font-family:Arial;font-size:13px;'>"
            f"<tr>{head}</tr>{''.join(body_rows)}</table>"
            "<p>The full ticket list is in the attached Excel file.</p>")


def post_eod_report(day=None):
    day = day or previous_day()
    parts = build_eod_report(day)
    url = report_settings.webhook_url()
    if not url:
        raise RuntimeError("No Teams workflow link is saved yet")
    resp = requests.post(url, json=workflow_payload(parts), timeout=60)
    resp.raise_for_status()
    print(f"[sla_report] End-of-day report for {day} posted to chat at {datetime.now(REPORT_TZ):%Y-%m-%d %H:%M %Z}")

    if report_settings.load()["email_eod_enabled"]:
        send_eod_email(day, parts)
        print(f"[sla_report] End-of-day email sent for {day}")
    return True


def next_eod(now):
    """The next 00:00:00 Jira time (America/New_York) after `now`. It reports on the day before."""
    midnight_today = now.astimezone(breach_logic.JIRA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_today + timedelta(days=1)


def email_slot_hours(interval):
    """Email times in Puerto Rico time from 08:00 to 23:00, every `interval` hours."""
    return list(range(8, 24, interval))


def next_email_slot(now, interval=1):
    hours = email_slot_hours(interval)
    for hour in hours:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    return (now + timedelta(days=1)).replace(hour=hours[0], minute=0, second=0, microsecond=0)


SCHEDULER_LOCK_PORT = 47613  # any free local port; only one dashboard process can hold it
_scheduler_lock = None


def claim_scheduler_lock():
    """True if this process now owns the report schedule. Only one dashboard
    process may run it, so a second open dashboard can't post the same report."""
    global _scheduler_lock
    if _scheduler_lock is not None:
        return True
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", SCHEDULER_LOCK_PORT))
    except OSError:
        sock.close()
        return False
    _scheduler_lock = sock  # keep it open for the life of this process
    return True


class ReportScheduler:
    """Background loop used by the dashboard. Posts the 2-hourly report while its
    switch is on, and the end-of-day report at 00:00:00 Jira time (for the day
    before) while its switch is on."""

    def __init__(self):
        self.status = {"next": None, "last": None, "last_error": None}

    def _record(self, error=None):
        if error is None:
            self.status["last"] = datetime.now(REPORT_TZ)
            self.status["last_error"] = None
        else:
            self.status["last_error"] = str(error)

    def send_now(self):
        try:
            post_report()
        except Exception as exc:
            self._record(exc)
            raise
        self._record()

    def send_eod_now(self, day=None):
        try:
            post_eod_report(day)
        except Exception as exc:
            self._record(exc)
            raise
        self._record()

    def run(self):
        while True:
            s = report_settings.load()
            now = datetime.now(REPORT_TZ)
            options = []
            if s["enabled"]:
                options.append((next_slot(now, report_settings.interval_hours(s)), "slot"))
            if s["eod_enabled"]:
                options.append((next_eod(now), "eod"))
            if s["email_hourly_enabled"]:
                options.append((next_email_slot(now, report_settings.email_interval_hours(s)), "email"))
            if not options:
                self.status["next"] = None
                time.sleep(30)
                continue
            when, kind = min(options)
            self.status["next"] = when
            # Wake every 30 seconds; re-plan if either switch changes.
            while datetime.now(REPORT_TZ) < when:
                cur = report_settings.load()
                if (cur["enabled"] != s["enabled"] or cur["eod_enabled"] != s["eod_enabled"]
                        or cur["email_hourly_enabled"] != s["email_hourly_enabled"]
                        or report_settings.email_interval_hours(cur) != report_settings.email_interval_hours(s)
                        or report_settings.interval_hours(cur) != report_settings.interval_hours(s)):
                    break
                time.sleep(min(30, max(1.0, (when - datetime.now(REPORT_TZ)).total_seconds())))
            cur = report_settings.load()
            still_on = {"eod": cur["eod_enabled"], "email": cur["email_hourly_enabled"]}.get(kind, cur["enabled"])
            if datetime.now(REPORT_TZ) < when or not still_on:
                continue
            try:
                if kind == "eod":
                    # Sent at 00:00:00 on the 20th, so the report covers the 19th.
                    self.send_eod_now(when.date() - timedelta(days=1))
                elif kind == "email":
                    send_hourly_email()
                    self._record()
                else:
                    self.send_now()
            except Exception as exc:
                print(f"[sla_report] Report failed: {exc}")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--now", action="store_true", help="post one report to the saved link and exit")
    parser.add_argument("--dry-run", action="store_true", help="print the message without posting")
    args = parser.parse_args()
    if args.now or args.dry_run:
        post_report(dry_run=args.dry_run)
    else:
        parser.print_help()
        print("\nThe scheduled report runs from the dashboard's Teams report panel.")
