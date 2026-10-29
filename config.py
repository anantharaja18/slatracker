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

# Timezone of the Jira profile of the JIRA_EMAIL account. Jira returns every
# timestamp in it and evaluates startOfDay() in it, so the reports' days and
# weeks follow it; the "Last refreshed" / "synced" times are shown in it too.
JIRA_TIMEZONE = os.environ.get("JIRA_TIMEZONE", "America/New_York")

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
# resolved_scope_jql -> every resolved ticket in the team's scope, any date,
#                  excluding cancelled tickets
#                  (Resolved History page); the dashboard's "resolved_jql"
#                  (resolved today) is built from it below
# inflow_jql     -> every ticket created today in the team's scope, whatever
#                   its current status ("Inflow Today" on the dashboard)
# backlog_jql    -> optional: open tickets created before today, for a team
#                   whose "jql" only covers today's tickets (dashboard "Backlog")
# ---------------------------------------------------------------------------
SEVERITY_FIELD = "customfield_10051"  # "Severity" custom field -- values are "Sev-1" / "Sev-2"

# SLA override applied to every category, measured from ticket Created time:
# Sev-1 = 1 hour, Sev-2 = 4 hours. Jira's own SLA timers are not used.
SLA_MINUTES = {"Sev-1": 60, "Sev-2": 120}

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
        "resolved_scope_jql": (
            'project = 10145 AND "Reporting Area[Dropdown]" = Retail '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND resolution is not EMPTY AND status NOT IN (Canceled, Cancelled)'
        ),
        "inflow_jql": (
            'project = 10145 AND "Reporting Area[Dropdown]" = Retail '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND created >= startofday() '
            'AND status NOT IN (Canceled, Cancelled)'
        ),
        "breach_type": "sla_from_created",
        "severity_field": SEVERITY_FIELD,
        "sla_minutes": SLA_MINUTES,
        "warn_hours": 2,
    },
    "SAC": {
        "jql": (
            'project = SAC AND created >= startofday() AND "Reporting Area" != Retail '
            'AND reporter != "SAC BOT" AND resolution is EMPTY '
            'AND status NOT IN ("Waiting for customer", Canceled, Cancelled) '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY)'
        ),
        "resolved_scope_jql": (
            'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND resolution is not EMPTY AND status NOT IN (Canceled, Cancelled)'
        ),
        "inflow_jql": (
            'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND created >= startofday() '
            'AND status NOT IN (Canceled, Cancelled)'
        ),
        # SAC's "jql" above only covers tickets created today, so its open
        # backlog (created before today, still open) needs its own query.
        "backlog_jql": (
            'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
            'AND ("Account type[Dropdown]" != "B2B" OR "Account type[Dropdown]" is EMPTY) '
            'AND status NOT IN (Resolved, Closed, "Waiting for customer", Canceled, Cancelled) '
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
            '(Resolved, Closed, Canceled, Cancelled, "Waiting for customer") ORDER BY created DESC'
        ),
        "resolved_scope_jql": (
            'project = SAC AND "Account type[Dropdown]" = "B2B" AND '
            '"Reporting Area[Dropdown]" != "Order Fallout" '
            'AND resolution is not EMPTY AND status NOT IN (Canceled, Cancelled)'
        ),
        "inflow_jql": (
            'project = SAC AND "Account type[Dropdown]" = "B2B" AND '
            '"Reporting Area[Dropdown]" != "Order Fallout" '
            'AND created >= startofday() '
            'AND status NOT IN (Canceled, Cancelled)'
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
        "resolved_scope_jql": (
            'project = SAC AND "Reporting Area[Dropdown]" = "Order fallout" '
            'AND resolution is not EMPTY AND status NOT IN (Canceled, Cancelled)'
        ),
        "inflow_jql": (
            'project = SAC AND "Reporting Area[Dropdown]" = "Order fallout" '
            'AND created >= startofday() '
            'AND status NOT IN (Canceled, Cancelled)'
        ),
        "breach_type": "sla_from_created",
        "severity_field": SEVERITY_FIELD,
        "sla_minutes": SLA_MINUTES,
        "warn_hours": 1,
    },
}

for _cat in CATEGORIES.values():
    _cat["resolved_jql"] = _cat["resolved_scope_jql"] + " AND resolutiondate >= startofday()"

# ---------------------------------------------------------------------------
# Ticket Trend Analysis (pages/Ticket_Trend_Analysis.py, trend_store.py)
# ---------------------------------------------------------------------------
TREND_HISTORY_DAYS = 366      # ~1 year of tickets kept in the local cache
TREND_SYNC_MINUTES = 10       # how often `python trend_store.py --loop` re-syncs
TREND_STALE_MINUTES = 15      # the page itself syncs if the cache is older than this
TREND_TOP_N = 5

TREND_FIELDS = {
    "reporting_area": "customfield_10861",        # Reporting Area
    "account_type": "customfield_10880",          # Account type (B2B / B2C)
    "incident_category": "customfield_10867",     # Incident Category
    "incident_subcategory": "customfield_10868",  # Incident Sub Category -- SAC / B2B driver
    "of_reason": "customfield_17928",             # Order Fallout Reason
    "of_step": "customfield_17930",               # Order Fallout Step (failed step)
    "of_type": "customfield_17929",               # Order Fallout Type
    "of_error": "customfield_17931",              # Order Fallout Error Description (rich text)
    "sat_issue": "customfield_11683",             # SAT: Issue Identified by SAT Agent (text copy)
    "sat_subcategory": "customfield_11684",       # SAT: Issue Sub-Category (text copy) -- SAT driver
    "sat_channel": "customfield_11804",           # SAT: Channel SAT (Retail Store / AR / Kiosk ...)
    "sat_order_type": "customfield_11671",        # SAT: Type of Order
    "sat_customer_type": "customfield_11672",     # SAT: Type of customer (Postpaid / Prepaid)
    "fix_eta": "customfield_16974",               # SAL "Final or Production ETA"
    "fix_source": "customfield_10052",            # SAL "Source" (e.g. Order Fallout B2C)
}

# Trend categories: each is exactly this JQL (the page adds the date range).
# Cancelled tickets are excluded everywhere in this app.
# A ticket matching several (e.g. a B2B order fallout) counts in each.
# Changing any of these (or the fields) makes the next sync reload the full
# history.
TREND_CATEGORY_JQL = {
    "SAC": (
        'project = SAC AND "Reporting Area" != Retail AND reporter != "SAC BOT" '
        'AND "Severity" IS NOT EMPTY AND status NOT IN (Canceled, Cancelled)'
    ),
    "B2B": 'project = SAC AND "Account type[Dropdown]" = "B2B" AND status NOT IN (Canceled, Cancelled)',
    "Order Fallout": (
        'project = SAC AND reporter = "SAC BOT" AND summary !~ "\\"POSSIBLE DUPLICATE\\"" '
        'AND text ~ "Order Fallout" AND status NOT IN (Canceled, Cancelled)'
    ),
    "SAT": (
        'project = SAT AND labels IS EMPTY '
        'AND "Issue Sub-Category (copia)" !~ "No Issue Found" '
        'AND "Issue Sub-Category (copia)" !~ "Seller Didnt Followed Process" '
        'AND "Issue Sub-Category (copia)" !~ "Waiting for Response" '
        'AND "Issue Sub-Category (copia)" !~ "Resolution Required" '
        'AND status NOT IN (Canceled, Cancelled)'
    ),
}

# Every project the categories draw from; used to spot tickets that changed
# since the last sync (so ones that left a category get dropped from it).
TREND_TICKET_JQL = "project in (SAC, SAT)"

# SAL (development fixes) and SFS (Salesforce support) tickets to look for
# fixes in, on top of any SAL / SFS ticket directly linked to a SAC ticket.
# Two years back, so fixes made before the oldest cached ticket still match.
TREND_FIX_JQLS = [
    'project = SAL AND created >= -730d AND (labels in ("L3.Peacock.B2B", "L3.Peacock.B2C", '
    '"L3.Peacock.Support[prodapt_dev]", "L3_Peacock_Support_Team", "LCPR.ProdStab.Roadmap", '
    '"L3.Peacock.ITOpsPriority", "L3.Peacock.RetailOperations", "L3.Peacock.RetailPriority", '
    '"LCPR_orderfallout_bot") OR Source is not EMPTY) AND status NOT IN (Canceled, Cancelled)',
    'project = SFS AND created >= -730d AND "Business Unit by OpCo" = "LCPR-Puerto Rico (Peacock)" '
    'AND status NOT IN (Canceled, Cancelled)',
]
