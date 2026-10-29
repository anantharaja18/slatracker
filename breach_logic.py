"""
Determines breach status (OK / About to Breach / Breached) for a Jira issue
based on that category's configured breach field and thresholds.
"""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from dateutil import parser as date_parser

import config

import jira_client


STATUS_OK = "OK"
STATUS_WARNING = "About to Breach"
STATUS_BREACHED = "Breached"
STATUS_UNKNOWN = "Unknown (no due date set)"


def _parse_datetime(value):
    if not value:
        return None
    try:
        dt = date_parser.parse(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt
    except (ValueError, TypeError):
        return None


JIRA_TZ = ZoneInfo(config.JIRA_TIMEZONE)


def jira_now():
    """Current time in the Jira timezone (not the PC clock's)."""
    return datetime.now(JIRA_TZ)


def to_jira_time(dt):
    """A PC-local time (naive) or any aware time, in the Jira timezone."""
    return (dt if dt.tzinfo else dt.astimezone()).astimezone(JIRA_TZ)


def jira_time_label():
    """e.g. "Jira time · America/New_York (EDT)" -- the zone the report uses."""
    return f"Jira time · {config.JIRA_TIMEZONE} ({jira_now():%Z})"


def format_due_datetime(dt):
    """Formats a due/resolved datetime in whatever timezone Jira reported it
    in (the authenticating account's Jira profile timezone) -- no conversion
    to UTC or the local machine's timezone -- with that offset shown explicitly
    so it's never ambiguous which timezone the time is in."""
    if dt is None:
        return "N/A"
    offset = dt.strftime("%z")
    if offset:
        return f"{dt.strftime('%Y-%m-%d %H:%M')} (UTC{offset[:3]}:{offset[3:]})"
    return dt.strftime("%Y-%m-%d %H:%M")


def _extract_option_value(raw):
    """Jira option-type fields come back as {"value": "..."} (or {"name": ...});
    plain text fields come back as a bare string."""
    if isinstance(raw, dict):
        return raw.get("value") or raw.get("name")
    return raw


def evaluate(issue: dict, category_config: dict):
    """
    Returns a dict: {status, due_date, hours_remaining, severity} for one issue.
    hours_remaining is negative if the deadline has already passed.
    """
    breach_type = category_config["breach_type"]
    severity = None

    if breach_type == "sla_from_created":
        created_raw = jira_client.get_field_value(issue, "created")
        created_dt = _parse_datetime(created_raw)

        severity_raw = jira_client.get_field_value(issue, category_config["severity_field"])
        severity = _extract_option_value(severity_raw)
        sla_minutes = category_config.get("sla_minutes", {}).get(severity)

        if created_dt is not None and sla_minutes is not None:
            due_dt = created_dt + timedelta(minutes=sla_minutes)
        else:
            due_dt = None
    elif breach_type == "date_field":
        raw_value = jira_client.get_field_value(issue, category_config["breach_field"])
        due_dt = _parse_datetime(raw_value)
    elif breach_type == "sla":
        # Placeholder: if you switch a category to breach_type "sla", wire
        # this up to the Jira Service Management SLA endpoint:
        #   GET /rest/servicedeskapi/request/{issueKey}/sla
        # and pull ongoingCycle.remainingTime.millis from the response.
        due_dt = None
    else:
        due_dt = None

    if due_dt is None:
        return {"status": STATUS_UNKNOWN, "due_date": None, "hours_remaining": None, "severity": severity}

    now = datetime.now(timezone.utc)
    delta_hours = (due_dt - now).total_seconds() / 3600.0

    if delta_hours < 0:
        status = STATUS_BREACHED
    elif delta_hours <= category_config["warn_hours"]:
        status = STATUS_WARNING
    else:
        status = STATUS_OK

    return {"status": status, "due_date": due_dt, "hours_remaining": round(delta_hours, 1), "severity": severity}


def evaluate_resolution(issue: dict, category_config: dict):
    """
    For an already-resolved issue, checks whether its resolution date fell
    within the configured SLA (created + sla_minutes[severity]).
    Returns a dict: {status, due_date, resolved_date, severity}.
    status is STATUS_OK ("resolved within SLA"), STATUS_BREACHED, or
    STATUS_UNKNOWN if the SLA or resolution date couldn't be determined.
    """
    created_raw = jira_client.get_field_value(issue, "created")
    created_dt = _parse_datetime(created_raw)

    severity_raw = jira_client.get_field_value(issue, category_config["severity_field"])
    severity = _extract_option_value(severity_raw)
    sla_minutes = category_config.get("sla_minutes", {}).get(severity)

    resolved_raw = jira_client.get_field_value(issue, "resolutiondate")
    resolved_dt = _parse_datetime(resolved_raw)

    if created_dt is None or sla_minutes is None or resolved_dt is None:
        return {"status": STATUS_UNKNOWN, "due_date": None, "resolved_date": resolved_dt, "severity": severity}

    due_dt = created_dt + timedelta(minutes=sla_minutes)
    status = STATUS_OK if resolved_dt <= due_dt else STATUS_BREACHED
    return {"status": status, "due_date": due_dt, "resolved_date": resolved_dt, "severity": severity}
