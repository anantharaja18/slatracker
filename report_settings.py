"""
Settings for the Teams report: the Workflow webhook link and the on/off switch.

Saved in report_settings.json in the project folder (git-ignored, like .env).
Set from the dashboard's "Teams report" panel. SLA_REPORT_FLOW_URL in .env is
still accepted as a fallback if no link has been saved in the panel.
"""

import json
import os
import threading

PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "report_settings.json")
DEFAULTS = {"webhook_url": "", "enabled": False, "eod_enabled": False, "interval_hours": 1,
            "email_hourly_enabled": False, "email_eod_enabled": False,
            "productivity_hourly_enabled": False, "productivity_eod_enabled": False,
            "email_recipients": None, "email_log": {}, "email_interval_hours": 1}  # None = use SMTP_TO from .env; a list = saved in the admin panel
INTERVAL_CHOICES = (1, 2, 3, 4)
_lock = threading.Lock()


def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        data = {}
    return {**DEFAULTS, **data}


def save(settings):
    with _lock:
        tmp = PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({**DEFAULTS, **settings}, f, indent=2)
        os.replace(tmp, PATH)


def interval_hours(settings=None):
    """How often the slot report runs, in hours (1, 2, 3 or 4). Anything else falls back to 1."""
    value = (settings or load()).get("interval_hours", 1)
    return value if value in INTERVAL_CHOICES else 1


def email_interval_hours(settings=None):
    """How often the email report runs, in hours (1, 2, 3 or 4). Anything else falls back to 1."""
    value = (settings or load()).get("email_interval_hours", 1)
    return value if value in INTERVAL_CHOICES else 1


def webhook_url():
    """The saved link, or the SLA_REPORT_FLOW_URL setting from .env."""
    saved = load()["webhook_url"].strip()
    return saved or os.environ.get("SLA_REPORT_FLOW_URL", "").strip()
