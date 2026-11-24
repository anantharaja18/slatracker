"""
A log of every report send (Teams and email), kept in report_log.json in the project
folder (git-ignored, like report_settings.json) and shown in the dashboard's
Report Settings panel. Each entry is also printed to the dashboard's terminal.

Error text never includes a web address: any link in an error is replaced with
"[link]", so a Teams workflow link (a secret) cannot end up in the log.
"""

import json
import os
import re
import threading
from datetime import datetime
from zoneinfo import ZoneInfo

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_log.json")
KEEP = 200
REPORT_TZ = ZoneInfo("America/Puerto_Rico")
_lock = threading.Lock()
_URL = re.compile(r"https?://\S+")

# Names shown in the log for the email sends (the keys are the email_log keys in sla_report).
EMAIL_NAMES = {
    "hourly": "SLA email (hourly)",
    "eod": "SLA email (end of day)",
    "productivity_hourly": "Productivity email (hourly)",
    "productivity_eod": "Productivity email (end of day)",
}


def clean(text):
    """Error text with any web address removed."""
    return _URL.sub("[link]", str(text))[:500]


def _read():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, ValueError):
        return []


def record(name, ok, detail):
    """Adds one send to the log. `name` is the report's name, e.g. "Teams report"."""
    entry = {"at": datetime.now(REPORT_TZ).strftime("%d %b %Y %H:%M:%S %Z"), "name": name,
             "ok": bool(ok), "detail": clean(detail)}
    print(f"[report_log] {entry['at']} | {name} | {'Sent' if ok else 'FAILED'} | {entry['detail']}")
    with _lock:
        entries = (_read() + [entry])[-KEEP:]
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(entries, f, indent=2)
        os.replace(tmp, PATH)


def recent(limit=KEEP):
    """The newest entries first."""
    with _lock:
        return list(reversed(_read()))[:limit]
