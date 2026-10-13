"""
Configuration for the Jira Breach Monitor.

Fill in your Jira credentials and Teams webhook URL below, then check the
per-category breach rules in CATEGORIES match your actual SLA/due-date
fields (marked with TODO where I've guessed based on what you shared).
"""

import os

from dotenv import load_dotenv

# Secrets live in a local .env file (gitignored) -- see .env.example.
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

# ---------------------------------------------------------------------------
# Jira connection
# ---------------------------------------------------------------------------
# You can either hardcode these directly, or set them as environment
# variables (recommended so the token isn't sitting in plain text in git).
JIRA_BASE_URL = os.environ.get("JIRA_BASE_URL", "https://lla.atlassian.net/")
JIRA_EMAIL = os.environ.get("JIRA_EMAIL", "")
JIRA_API_TOKEN = os.environ.get("JIRA_API_TOKEN", "")

# ---------------------------------------------------------------------------
# Microsoft Teams
# ---------------------------------------------------------------------------
# Simplest working setup: an Incoming Webhook connector on a Teams channel.
# Create one: open the Teams channel -> "..." -> Connectors -> Incoming
# Webhook -> name it -> copy the URL it gives you into here.
TEAMS_CHANNEL_WEBHOOK_URL = os.environ.get(
    "TEAMS_CHANNEL_WEBHOOK_URL", ""
)

# True 1:1 DMs to the assignee require a Microsoft Graph API app
# registration (tenant id / client id / client secret) with Chat.Create and
# ChatMessage.Send permissions. Until that's set up, GRAPH_ENABLED stays
# False and notifications go to the shared channel with the assignee named
# in the message text instead of a real @mention/DM.
GRAPH_ENABLED = False
GRAPH_TENANT_ID = os.environ.get("GRAPH_TENANT_ID", "")
GRAPH_CLIENT_ID = os.environ.get("GRAPH_CLIENT_ID", "")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")

# ---------------------------------------------------------------------------
# Claude (dashboard chat assistant -- see ai_assistant.py)
# ---------------------------------------------------------------------------
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5")

# ---------------------------------------------------------------------------
# Dedup / state file
# ---------------------------------------------------------------------------
# Tracks which (ticket, status) pairs we've already alerted on, so the
# checker doesn't re-notify every single run once a ticket is flagged.
STATE_FILE = "notified_state.json"

# ---------------------------------------------------------------------------
# Scheduler (used by checker.py)
# ---------------------------------------------------------------------------
CHECK_INTERVAL_MINUTES = 30

# ---------------------------------------------------------------------------
# Categories: JQL + breach rule
#
# breach_type:
#   "sla_from_created" -> deadline = Created + sla_minutes[severity value],
#                          where the severity value comes from severity_field
#                          (the "Severity" custom field, e.g. "Sev-1"/"Sev-2").
#                          A severity with no entry in sla_minutes has no
#                          configured SLA and shows as Unknown rather than
#                          guessing a deadline.
#   "date_field"        -> compare now() to a date/datetime field on the issue
#                          (legacy; kept for categories that may want it later)
#
# severity_field -> Jira field ID holding the severity value (shared across
#                   categories; override per-category only if it differs)
# sla_minutes    -> {severity value: minutes to resolve, measured from Created}
# warn_hours     -> how many hours before the deadline counts as
#                   "About to Breach" (breach itself = deadline has passed)
# inflow_jql     -> every ticket created today in the team's scope, whatever
#                   its current status ("Inflow Today" on the dashboard)
# backlog_jql    -> optional: open tickets created before today, for a team
#                   whose "jql" only covers today's tickets (dashboard "Backlog")
# ---------------------------------------------------------------------------
SEVERITY_FIELD = "customfield_10051"  # "Severity" custom field -- values are "Sev-1" / "Sev-2"

# SLA override applied to every category, measured from ticket Created time:
# Sev-1 = 1 hour, Sev-2 = 4 hours. Jira's own SLA timers are not used.
SLA_MINUTES = {"Sev-1": 60, "Sev-2": 240}

# B2B tickets are counted only under B2B: Retail and SAC exclude them (keeping
# tickets with no Account type) so no ticket shows up under two teams.
CATEGORIES = {
    "Retail": {
        "jql": (
            'project = 10145 AND status in ("Waiting for support", '
            '"In Progress", Escalated) AND "Reporting Area[Dropdown]" = Retail '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'ORDER BY created DESC, status ASC, assignee ASC, "cf[10883]" DESC, key DESC'
        ),
        "resolved_jql": (
            'project = 10145 AND "Reporting Area[Dropdown]" = Retail '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND resolution is not EMPTY AND resolutiondate >= startofday()'
        ),
        "inflow_jql": (
            'project = 10145 AND "Reporting Area[Dropdown]" = Retail '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND created >= startofday()'
        ),
        "breach_type": "sla_from_created",
        "severity_field": SEVERITY_FIELD,
        "sla_minutes": SLA_MINUTES,
        "warn_hours": 2,
    },
    "SAC": {
        "jql": (
            'project = SAC AND created >= startofday() AND "Reporting Area" != Retail '
            'AND reporter != "SAC BOT" AND resolution is EMPTY AND status != "Waiting for customer" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY)'
        ),
        "resolved_jql": (
            'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND resolution is not EMPTY AND resolutiondate >= startofday()'
        ),
        "inflow_jql": (
            'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND created >= startofday()'
        ),
        # SAC's "jql" above only covers tickets created today, so its open
        # backlog (created before today, still open) needs its own query.
        "backlog_jql": (
            'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND status NOT IN (Resolved, Closed, "Waiting for customer", Cancelled) '
            'AND created <= startOfDay()'
        ),
        "breach_type": "sla_from_created",
        "severity_field": SEVERITY_FIELD,
        "sla_minutes": SLA_MINUTES,
        "warn_hours": 2,
    },
    "B2B": {
        "jql": (
            'project = SAC AND "Account type[Dropdown]" = "B2B" AND '
            '"Reporting Area[Dropdown]" != "Order Fallout" AND status not in '
            '(Resolved, Closed, Cancelled, "Waiting for customer") ORDER BY created DESC'
        ),
        "resolved_jql": (
            'project = SAC AND "Account type[Dropdown]" = "B2B" AND '
            '"Reporting Area[Dropdown]" != "Order Fallout" '
            'AND resolution is not EMPTY AND resolutiondate >= startofday()'
        ),
        "inflow_jql": (
            'project = SAC AND "Account type[Dropdown]" = "B2B" AND '
            '"Reporting Area[Dropdown]" != "Order Fallout" '
            'AND created >= startofday()'
        ),
        "breach_type": "sla_from_created",
        "severity_field": SEVERITY_FIELD,
        "sla_minutes": SLA_MINUTES,
        "warn_hours": 4,
    },
    "Order Fallout": {
        "jql": (
            'project = SAC AND status in ("In Progress", "Waiting for support", Escalated) '
            'AND "Reporting Area[Dropdown]" = "Order fallout"'
        ),
        "resolved_jql": (
            'project = SAC AND "Reporting Area[Dropdown]" = "Order fallout" '
            'AND resolution is not EMPTY AND resolutiondate >= startofday()'
        ),
        "inflow_jql": (
            'project = SAC AND "Reporting Area[Dropdown]" = "Order fallout" '
            'AND created >= startofday()'
        ),
        "breach_type": "sla_from_created",
        "severity_field": SEVERITY_FIELD,
        "sla_minutes": SLA_MINUTES,
        "warn_hours": 1,
    },
}
