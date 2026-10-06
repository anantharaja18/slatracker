"""
One-off diagnostic: checks Jira auth and prints raw issue counts for each
category's JQL (both the "open" and "resolved" queries), without going
through the dashboard's cached/evaluated layer.

Run it in the SAME terminal/session where JIRA_EMAIL / JIRA_API_TOKEN are
set (wherever you launched streamlit from):

    python diagnose_jira.py
"""

import sys

import requests
from requests.auth import HTTPBasicAuth

import config
import jira_client


def check_auth():
    print("=== Auth check (/rest/api/3/myself) ===")
    if not config.JIRA_EMAIL or not config.JIRA_API_TOKEN:
        print("JIRA_EMAIL or JIRA_API_TOKEN is empty in this process's environment.")
        return False
    url = f"{config.JIRA_BASE_URL.rstrip('/')}/rest/api/3/myself"
    resp = requests.get(url, auth=HTTPBasicAuth(config.JIRA_EMAIL, config.JIRA_API_TOKEN), timeout=15)
    print(f"Status: {resp.status_code}")
    if resp.status_code == 200:
        me = resp.json()
        print(f"Authenticated as: {me.get('displayName')} <{me.get('emailAddress')}>")
        return True
    else:
        print(f"Response body: {resp.text[:500]}")
        return False


def check_jql(label, jql):
    print(f"\n=== {label} ===")
    print(f"JQL: {jql}")
    try:
        issues = jira_client.search_issues(jql, fields=["summary"], max_results=50)
        print(f"-> {len(issues)} issue(s) returned")
        for issue in issues[:5]:
            print(f"   {issue['key']}: {issue['fields'].get('summary', '')}")
    except requests.HTTPError as exc:
        print(f"-> HTTP error: {exc}")
        print(f"   Response body: {exc.response.text[:1000]}")
    except Exception as exc:
        print(f"-> Unexpected error: {exc}")


def main():
    ok = check_auth()
    if not ok:
        print("\nAuth failed -- fix credentials before checking JQL results.")
        sys.exit(1)

    for category, cat_config in config.CATEGORIES.items():
        check_jql(f"{category} - open", cat_config["jql"])
        check_jql(f"{category} - resolved today", cat_config["resolved_jql"])


if __name__ == "__main__":
    main()
