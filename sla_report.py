"""
Posts the dashboard's "SLA by Severity -- Today's Inflow" table to the Teams
channel (config.TEAMS_CHANNEL_WEBHOOK_URL) every 2 hours from 08:00 to 22:00
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

REPORT_TZ = ZoneInfo("America/Puerto_Rico")
SLOT_HOURS = list(range(8, 23, 2))  # 08:00, 10:00, ..., 22:00 Puerto Rico time
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
                    rows.append({"Team": team, "Key": issue["key"], "Severity": result["severity"] or "N/A"})
    return pd.DataFrame(rows, columns=["Team", "Key", "Severity"])


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


def format_table(rows):
    """Fixed-width table for a Teams code block."""
    head = f"{'Team':<14}{'Sev':<7}{'Resolved':>9}{'Within':>8}{'Late':>6}{'Open':>6}{'SLA %':>8}"
    lines = [head, "-" * len(head)]
    for i, r in enumerate(rows):
        if r["Team"] == "All teams" and rows[i - 1]["Team"] != "All teams":
            lines.append("-" * len(head))
        pct = "—" if r["SLA"] is None else f"{r['SLA']:.1f}%"
        lines.append(f"{r['Team']:<14}{r['Sev']:<7}{r['Resolved']:>9}{r['Within']:>8}"
                     f"{r['Late']:>6}{r['Open']:>6}{pct:>8}")
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
                     "Open = open tickets that breached their SLA today. "
                     "SLA % = within ÷ (within + late + open)."),
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

    header = ["Team", "Sev", "Resolved", "Within", "Late", "Open", "SLA %"]
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
        values = [r["Team"], r["Sev"], r["Resolved"], r["Within"], r["Late"], r["Open"], pct]
        cells = [cell(v, i, bold=is_all, emphasis=is_all, color=color if i == 6 else None)
                 for i, v in enumerate(values)]
        table_rows.append({"type": "TableRow", "cells": cells})

    table = {
        "type": "Table",
        "gridStyle": "accent",
        "showGridLines": True,
        "firstRowAsHeader": True,
        "verticalCellContentAlignment": "center",
        # Widths are shares of the card width (wide enough that no label is cut off):
        # Team 7, Sev 4, Resolved 5, Within 4, Late 4, Open 5, SLA % 5.
        "columns": [{"width": 7}, {"width": 4}, {"width": 5}, {"width": 4}, {"width": 4}, {"width": 5}, {"width": 5}],
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


def next_slot(now):
    """The next 08:00-22:00 (every 2 hours) slot after `now`, Puerto Rico time."""
    for hour in SLOT_HOURS:
        slot = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if slot > now:
            return slot
    tomorrow = now + timedelta(days=1)
    return tomorrow.replace(hour=SLOT_HOURS[0], minute=0, second=0, microsecond=0)


EOD_FIELDS = ["summary", "status", "assignee", "created", "resolutiondate", config.SEVERITY_FIELD,
              breach_logic.JIRA_SLA_FIELD]
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
            })
    return pd.DataFrame(rows, columns=["Team", "Key", "Summary", "Severity", "Status", "Assignee",
                                       "Created", "Resolved", "Resolved Status", "Created Today"])


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
    })
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


def email_subject(day):
    return f"Daily Tickets Resolution & SLA Report - {ordinal_day(day)}"


def email_html(parts):
    """The summary table as an HTML email body (no local file paths)."""
    cell_style = "padding:4px 8px;border:1px solid #cccccc;"
    head = "".join(f"<th style='{cell_style}background:#20202c;color:#ffffff;'>{h}</th>"
                   for h in ["Team", "Sev", "Resolved", "Within SLA", "Resolved late", "Open", "SLA %"])
    body_rows = []
    for r in parts["rows"]:
        if r["Team"] == "Order Fallout" and r["Sev"] == "Sev-1" and not (r["Resolved"] or r["Within"] or r["Late"]):
            continue
        pct = "—" if r["SLA"] is None else f"{r['SLA']:.1f}%"
        bold = "font-weight:bold;" if r["Team"] == "All teams" else ""
        values = [r["Team"], r["Sev"], r["Resolved"], r["Within"], r["Late"], r["Open"], pct]
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

    email_url = os.environ.get("EOD_EMAIL_FLOW_URL", "").strip()
    if email_url:
        with open(parts["path"], "rb") as f:
            content = base64.b64encode(f.read()).decode("ascii")
        payload = {
            "subject": email_subject(day),
            "body": email_html(parts),
            "file_name": os.path.basename(parts["path"]),
            "file_content": content,
        }
        resp = requests.post(email_url, json=payload, timeout=120)
        resp.raise_for_status()
        print(f"[sla_report] End-of-day email sent for {day}")
    return True


def next_eod(now):
    """The next 00:00:00 Jira time (America/New_York) after `now`. It reports on the day before."""
    midnight_today = now.astimezone(breach_logic.JIRA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight_today + timedelta(days=1)


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
                options.append((next_slot(now), "slot"))
            if s["eod_enabled"]:
                options.append((next_eod(now), "eod"))
            if not options:
                self.status["next"] = None
                time.sleep(30)
                continue
            when, kind = min(options)
            self.status["next"] = when
            # Wake every 30 seconds; re-plan if either switch changes.
            while datetime.now(REPORT_TZ) < when:
                cur = report_settings.load()
                if cur["enabled"] != s["enabled"] or cur["eod_enabled"] != s["eod_enabled"]:
                    break
                time.sleep(min(30, max(1.0, (when - datetime.now(REPORT_TZ)).total_seconds())))
            cur = report_settings.load()
            still_on = cur["eod_enabled"] if kind == "eod" else cur["enabled"]
            if datetime.now(REPORT_TZ) < when or not still_on:
                continue
            try:
                if kind == "eod":
                    # Sent at 00:00:00 on the 20th, so the report covers the 19th.
                    self.send_eod_now(when.date() - timedelta(days=1))
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
        print("\nThe 2-hourly schedule runs from the dashboard's Teams report panel.")
