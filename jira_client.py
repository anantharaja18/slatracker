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
    for attempt, wait in enumerate([*RETRY_WAITS, None]):
        try:
            resp = requests.post(url, json=payload, auth=_auth(), timeout=60)
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


def get_field_value(issue: dict, field_id: str):
    """Safely pull a field value (custom or standard) off a raw issue dict."""
    return issue.get("fields", {}).get(field_id)
