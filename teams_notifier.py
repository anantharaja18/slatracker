"""
Sends notifications to Microsoft Teams.

Default mode: post to a shared channel via an Incoming Webhook, naming the
assignee in the message text (a plain webhook can't trigger a real
@mention/DM notification to that person -- it just names them).

If you later set up a Microsoft Graph API app registration, implement
send_direct_message() below using the Graph /chats endpoint and set
GRAPH_ENABLED = True in config.py to also message the assignee 1:1.
"""

import requests

import breach_logic
import config


def _build_card(issue_key, summary, category, assignee, status, hours_remaining, due_date, url):
    if hours_remaining is not None and hours_remaining < 0:
        remaining_text = f"{abs(hours_remaining):.1f} hrs overdue"
    elif hours_remaining is not None:
        remaining_text = f"{hours_remaining:.1f} hrs remaining"
    else:
        remaining_text = "No due date set"

    text = (
        f"**[{status}] {category} — {issue_key}**\n\n"
        f"**Summary:** {summary}\n\n"
        f"**Assignee:** {assignee or 'Unassigned'}\n\n"
        f"**Due:** {breach_logic.format_due_datetime(due_date)} "
        f"({remaining_text})\n\n"
        f"[Open ticket]({url})"
    )

    return {
        "@type": "MessageCard",
        "@context": "http://schema.org/extensions",
        "themeColor": "D93025" if status == "Breached" else "FFA500",
        "summary": f"{status}: {issue_key}",
        "text": text,
    }


def notify_channel(issue_key, summary, category, assignee, status, hours_remaining, due_date):
    if not config.TEAMS_CHANNEL_WEBHOOK_URL or "PASTE_YOUR" in config.TEAMS_CHANNEL_WEBHOOK_URL:
        print(f"[teams_notifier] Webhook not configured -- skipping alert for {issue_key}")
        return False

    url = f"{config.JIRA_BASE_URL.rstrip('/')}/browse/{issue_key}"
    card = _build_card(issue_key, summary, category, assignee, status, hours_remaining, due_date, url)

    resp = requests.post(config.TEAMS_CHANNEL_WEBHOOK_URL, json=card, timeout=15)
    resp.raise_for_status()
    return True


def send_direct_message(assignee_email, issue_key, summary, category, status, hours_remaining, due_date):
    """
    Placeholder for true 1:1 Teams DMs via Microsoft Graph API.
    Requires an app registration with Chat.Create and ChatMessage.Send
    permissions, plus a lookup from Jira assignee email -> Azure AD user id.
    """
    if not config.GRAPH_ENABLED:
        return False

    raise NotImplementedError(
        "Graph API DM sending isn't implemented yet. Add your app credentials "
        "to config.py (GRAPH_TENANT_ID / GRAPH_CLIENT_ID / GRAPH_CLIENT_SECRET), "
        "set GRAPH_ENABLED = True, and implement this function using the "
        "Microsoft Graph /chats endpoint."
    )
