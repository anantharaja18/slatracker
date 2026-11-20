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
import html
import os
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

RESOLVED_FIELDS = ["summary", "status", "assignee", "created", "resolutiondate", config.SEVERITY_FIELD]
OPEN_FIELDS = ["summary", "status", "assignee", "created", config.SEVERITY_FIELD]


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
        if r["Team"] == "Order Fallout" and r["Sev"] == "Sev-1" and not (r["Resolved"] or r["Open"]):
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


class ReportScheduler:
    """Background loop used by the dashboard: posts at each 2-hour slot while the
    on/off switch (report_settings) is on. Only one instance should run."""

    def __init__(self):
        self.status = {"next": None, "last": None, "last_error": None}

    def send_now(self):
        try:
            post_report()
        except Exception as exc:
            self.status["last_error"] = str(exc)
            raise
        self.status["last"] = datetime.now(REPORT_TZ)
        self.status["last_error"] = None

    def run(self):
        while True:
            if not report_settings.load()["enabled"]:
                self.status["next"] = None
                time.sleep(30)
                continue
            slot = next_slot(datetime.now(REPORT_TZ))
            self.status["next"] = slot
            # Wake up every 30 seconds so a switch change is noticed quickly.
            while report_settings.load()["enabled"] and datetime.now(REPORT_TZ) < slot:
                time.sleep(min(30, max(1.0, (slot - datetime.now(REPORT_TZ)).total_seconds())))
            if not report_settings.load()["enabled"]:
                continue
            try:
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
