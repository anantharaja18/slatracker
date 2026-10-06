"""
Tracks which (issue_key, status) pairs have already triggered a Teams
alert, so the checker doesn't re-notify every run once a ticket is flagged.
Notification fires again only if the status changes (e.g. Warning -> Breached).
"""

import json
import os

import config


def load_state():
    if not os.path.exists(config.STATE_FILE):
        return {}
    try:
        with open(config.STATE_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def save_state(state):
    with open(config.STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def already_notified(state, issue_key, status):
    return state.get(issue_key) == status


def mark_notified(state, issue_key, status):
    state[issue_key] = status
