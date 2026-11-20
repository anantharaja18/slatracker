"""
Thin wrapper around the Jira Cloud REST API for running JQL searches.
"""

import time

import requests
import truststore
from requests.auth import HTTPBasicAuth

import config

# Use the OS certificate store (Windows) instead of the bundled certifi list,
# so corporate TLS-inspecting proxies whose root CA is trusted by Windows
# are also trusted here -- avoids SSLCertVerificationError against corp networks.
truststore.inject_into_ssl()


def _auth():
    return HTTPBasicAuth(config.JIRA_EMAIL, config.JIRA_API_TOKEN)


# Timeouts, dropped connections, rate limits (429) and Jira 5xx errors are
# usually transient: retry them a few times with a growing pause.
RETRY_STATUS = {429, 500, 502, 503, 504}
RETRY_WAITS = [2, 5, 15, 30]


def _post_with_retry(url, payload):
    return _request_with_retry("POST", url, json=payload)


def _request_with_retry(method, url, **kwargs):
    for attempt, wait in enumerate([*RETRY_WAITS, None]):
        try:
            resp = requests.request(method, url, auth=_auth(), timeout=60, **kwargs)
            if resp.status_code not in RETRY_STATUS or wait is None:
                resp.raise_for_status()
                return resp.json()
            wait = max(wait, int(resp.headers.get("Retry-After", 0) or 0))
        except (requests.ConnectionError, requests.Timeout):
            if wait is None:
                raise
        time.sleep(wait)


def search_issues(jql: str, fields=None, max_results: int = 100):
    """
    Run a JQL search and return the list of raw issue dicts.
    Handles pagination automatically.

    Uses the enhanced JQL search endpoint (POST /rest/api/3/search/jql) --
    the older GET /rest/api/3/search endpoint was deprecated by Atlassian
    and now returns 410 Gone. The new endpoint pages via nextPageToken
    instead of startAt/total.
    """
    if fields is None:
        fields = ["summary", "status", "assignee", "priority", "created", "duedate"]

    url = f"{config.JIRA_BASE_URL.rstrip('/')}/rest/api/3/search/jql"
    issues = []
    next_page_token = None

    while True:
        payload = {
            "jql": jql,
            "fields": fields,
            "maxResults": max_results,
        }
        if next_page_token:
            payload["nextPageToken"] = next_page_token

        data = _post_with_retry(url, payload)

        issues.extend(data.get("issues", []))
        next_page_token = data.get("nextPageToken")

        if data.get("isLast", True) or not next_page_token:
            break

    return issues


def get_changelog(key: str):
    """Every changelog entry of one issue, oldest first."""
    url = f"{config.JIRA_BASE_URL.rstrip('/')}/rest/api/3/issue/{key}/changelog"
    histories, start = [], 0
    while True:
        data = _request_with_retry("GET", url, params={"startAt": start, "maxResults": 100})
        histories.extend(data.get("values", []))
        start += len(data.get("values", []))
        if data.get("isLast", True) or not data.get("values"):
            break
    return sorted(histories, key=lambda h: h["created"])


def assignee_name(issue: dict, fallback: str = "Unassigned"):
    """The issue's assignee; for an unassigned issue, whoever worked it, from
    the changelog: the last person it was assigned to, else the first person
    (not an automation) who moved it to In Progress."""
    assignee = (issue["fields"].get("assignee") or {}).get("displayName")
    if assignee:
        return assignee
    histories = get_changelog(issue["key"])
    last_assigned = None
    first_worker = None
    for h in histories:
        author = h.get("author") or {}
        for item in h.get("items", []):
            if item.get("field") == "assignee" and item.get("toString"):
                last_assigned = item["toString"]
            elif (item.get("field") == "status" and item.get("toString") == "In Progress"
                  and first_worker is None and author.get("accountType") != "app"):
                first_worker = author.get("displayName")
    return last_assigned or first_worker or fallback


def get_field_value(issue: dict, field_id: str):
    """Safely pull a field value (custom or standard) off a raw issue dict."""
    return issue.get("fields", {}).get(field_id)
