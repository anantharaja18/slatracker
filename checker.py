"""
Scheduled checker: runs the 4 category JQL queries, evaluates breach status,
and sends Teams alerts for newly Warning/Breached tickets.

Run continuously (it sleeps between checks based on config.CHECK_INTERVAL_MINUTES):
    python checker.py

Or set config.CHECK_INTERVAL_MINUTES = 0 to run a single pass and let an
external scheduler (cron / Windows Task Scheduler) call it repeatedly.
"""

import time
from datetime import datetime

import config
import jira_client
import breach_logic
import teams_notifier
import state as state_store


def run_once():
    state = state_store.load_state()
    print(f"\n[{datetime.now().isoformat()}] Running breach check...")

    for category, cat_config in config.CATEGORIES.items():
        fields = ["summary", "status", "assignee", "priority", "created"]
        if cat_config["breach_type"] == "sla_from_created":
            fields.append(cat_config["severity_field"])
        elif cat_config["breach_type"] == "date_field":
            fields.append(cat_config["breach_field"])
        try:
            issues = jira_client.search_issues(cat_config["jql"], fields=fields)
        except Exception as exc:
            print(f"  [{category}] ERROR fetching issues: {exc}")
            continue

        print(f"  [{category}] {len(issues)} ticket(s) matched")

        for issue in issues:
            key = issue["key"]
            summary = issue["fields"].get("summary", "")
            assignee_field = issue["fields"].get("assignee") or {}
            assignee_name = assignee_field.get("displayName")
            assignee_email = assignee_field.get("emailAddress")

            result = breach_logic.evaluate(issue, cat_config)
            status = result["status"]

            if status not in (breach_logic.STATUS_WARNING, breach_logic.STATUS_BREACHED):
                continue

            if state_store.already_notified(state, key, status):
                continue  # already alerted for this exact status

            print(f"    -> {key} is {status} (assignee: {assignee_name})")

            teams_notifier.notify_channel(
                issue_key=key,
                summary=summary,
                category=category,
                assignee=assignee_name,
                status=status,
                hours_remaining=result["hours_remaining"],
                due_date=result["due_date"],
            )

            if config.GRAPH_ENABLED and assignee_email:
                try:
                    teams_notifier.send_direct_message(
                        assignee_email, key, summary, category, status,
                        result["hours_remaining"], result["due_date"],
                    )
                except NotImplementedError as exc:
                    print(f"    [DM skipped] {exc}")

            state_store.mark_notified(state, key, status)

    state_store.save_state(state)


if __name__ == "__main__":
    if config.CHECK_INTERVAL_MINUTES <= 0:
        run_once()
    else:
        while True:
            run_once()
            print(f"Sleeping {config.CHECK_INTERVAL_MINUTES} minute(s)...")
            time.sleep(config.CHECK_INTERVAL_MINUTES * 60)
