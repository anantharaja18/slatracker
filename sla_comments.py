"""
SLA miss comments: one saved note per ticket, written by engineers from the dashboard
or the Historical Tickets page.

Stored in trend_data/sla_comments.db next to the trend cache. Every dashboard and
history page on this PC reads the same file, so a note saved in one shows in the other.
"""

import os
import sqlite3
from datetime import datetime

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_data", "sla_comments.db")


def _connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("CREATE TABLE IF NOT EXISTS sla_comments ("
                 "key TEXT PRIMARY KEY, comment TEXT NOT NULL, updated_at TEXT NOT NULL)")
    return conn


def load_all():
    """{ticket key: {"comment": text, "updated_at": "YYYY-MM-DD HH:MM"}}"""
    conn = _connect()
    try:
        rows = conn.execute("SELECT key, comment, updated_at FROM sla_comments").fetchall()
    finally:
        conn.close()
    return {key: {"comment": comment, "updated_at": updated} for key, comment, updated in rows}


def save(key, comment):
    """Saves (or replaces) the comment for one ticket. An empty comment is kept as empty."""
    conn = _connect()
    try:
        conn.execute("INSERT OR REPLACE INTO sla_comments (key, comment, updated_at) VALUES (?, ?, ?)",
                     (key, comment.strip(), datetime.now().strftime("%Y-%m-%d %H:%M")))
        conn.commit()
    finally:
        conn.close()
