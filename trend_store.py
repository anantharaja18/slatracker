"""
Local cache of Jira tickets for the Ticket Trend Analysis page.

Keeps the last TREND_HISTORY_DAYS of SAC-project tickets plus the SAL / SFS
fix tickets they may relate to in a SQLite file, so the trend page can
slice months of data by day / week / custom range without re-querying Jira.

    python trend_store.py           # one sync (full 6-month load on first run)
    python trend_store.py --loop    # sync agent: keeps the cache live, every
                                    # config.TREND_SYNC_MINUTES

The first run loads ~6 months (~35k tickets, a few minutes); every later run
only fetches tickets updated since the previous sync.
"""

import argparse
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta

import config
import jira_client

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_data", "trend_cache.db")

TICKET_FIELDS = [
    "summary", "created", "resolutiondate", "status", "reporter", "issuelinks",
    config.TREND_FIELDS["reporting_area"], config.TREND_FIELDS["account_type"],
    config.TREND_FIELDS["incident_category"], config.TREND_FIELDS["incident_subcategory"],
    config.TREND_FIELDS["of_reason"], config.TREND_FIELDS["of_step"],
    config.TREND_FIELDS["of_type"], config.TREND_FIELDS["of_error"], config.SEVERITY_FIELD,
]
FIX_FIELDS = [
    "summary", "status", "created", "resolutiondate", "duedate", "fixVersions",
    "issuelinks", "labels", config.TREND_FIELDS["fix_eta"], config.TREND_FIELDS["fix_source"],
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    key TEXT PRIMARY KEY, created TEXT, created_date TEXT, resolved TEXT, status TEXT,
    reporter TEXT, reporting_area TEXT, account_type TEXT, inc_cat TEXT, inc_subcat TEXT,
    of_step TEXT, of_reason TEXT, of_type TEXT, of_error TEXT, severity TEXT,
    summary TEXT, links TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_date);
CREATE TABLE IF NOT EXISTS fixes (
    key TEXT PRIMARY KEY, project TEXT, summary TEXT, status TEXT, status_cat TEXT,
    created TEXT, resolved TEXT, duedate TEXT, eta TEXT, versions TEXT, links TEXT,
    labels TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")  # page reads while the sync agent writes
    conn.executescript(SCHEMA)
    return conn


def get_meta(conn, k, default=None):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (k,)).fetchone()
    return row[0] if row else default


def set_meta(conn, k, v):
    conn.execute("INSERT OR REPLACE INTO meta (k, v) VALUES (?, ?)", (k, str(v)))


# ---------------------------------------------------------------------------
# Field flattening
# ---------------------------------------------------------------------------
def _option(v):
    if isinstance(v, dict):
        return v.get("value") or v.get("name") or ""
    return v or ""


def adf_to_text(node):
    """Flattens an Atlassian Document Format value (rich-text fields) to text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return " ".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return str(node)
    text = node.get("text", "")
    children = " ".join(adf_to_text(c) for c in node.get("content") or [])
    return " ".join(p for p in (text, children) if p).strip()


def _link_keys(fields):
    keys = []
    for link in fields.get("issuelinks") or []:
        other = link.get("outwardIssue") or link.get("inwardIssue")
        if other and other.get("key") and other["key"] not in keys:
            keys.append(other["key"])
    return keys


def ticket_row(issue):
    f = issue["fields"]
    tf = config.TREND_FIELDS
    created = f.get("created") or ""
    return (
        issue["key"], created,
        created[:10],  # date in the timezone Jira reported it in (same day boundary as JQL)
        f.get("resolutiondate") or "",
        (f.get("status") or {}).get("name", ""),
        (f.get("reporter") or {}).get("displayName", ""),
        _option(f.get(tf["reporting_area"])),
        _option(f.get(tf["account_type"])),
        _option(f.get(tf["incident_category"])),
        _option(f.get(tf["incident_subcategory"])),
        (f.get(tf["of_step"]) or "").strip(),
        (f.get(tf["of_reason"]) or "").strip(),
        (f.get(tf["of_type"]) or "").strip(),
        adf_to_text(f.get(tf["of_error"])).strip()[:1000],
        _option(f.get(config.SEVERITY_FIELD)),
        f.get("summary") or "",
        json.dumps(_link_keys(f)),
    )


def fix_row(issue):
    f = issue["fields"]
    status = f.get("status") or {}
    versions = [
        {"name": v.get("name"), "released": bool(v.get("released")), "releaseDate": v.get("releaseDate")}
        for v in f.get("fixVersions") or []
    ]
    return (
        issue["key"], issue["key"].split("-")[0], f.get("summary") or "",
        status.get("name", ""), (status.get("statusCategory") or {}).get("key", ""),
        f.get("created") or "", f.get("resolutiondate") or "", f.get("duedate") or "",
        (f.get(config.TREND_FIELDS["fix_eta"]) or "")[:10],
        json.dumps(versions), json.dumps(_link_keys(f)), json.dumps(f.get("labels") or []),
        _option(f.get(config.TREND_FIELDS["fix_source"])),
    )


def _upsert_tickets(conn, issues):
    conn.executemany("INSERT OR REPLACE INTO tickets VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [ticket_row(i) for i in issues])


def _upsert_fixes(conn, issues):
    conn.executemany("INSERT OR REPLACE INTO fixes VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                     [fix_row(i) for i in issues])


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------
def _weekly_windows(start, end):
    windows = []
    cur = start
    while cur <= end:
        nxt = cur + timedelta(days=7)
        windows.append((cur, nxt))
        cur = nxt
    return windows


def _full_ticket_load(conn, progress=None):
    """Loads TREND_HISTORY_DAYS of tickets in weekly slices, several at a time."""
    start = date.today() - timedelta(days=config.TREND_HISTORY_DAYS)
    windows = _weekly_windows(start, date.today())

    def fetch(window):
        a, b = window
        jql = f'{config.TREND_TICKET_JQL} AND created >= "{a:%Y-%m-%d}" AND created < "{b:%Y-%m-%d}"'
        return jira_client.search_issues(jql, fields=TICKET_FIELDS)

    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for issues in pool.map(fetch, windows):
            _upsert_tickets(conn, issues)
            conn.commit()
            done += 1
            if progress:
                progress(done / len(windows), f"Loaded {done}/{len(windows)} weeks of tickets")


def _fetch_by_keys(keys):
    issues = []
    keys = sorted(keys)
    for i in range(0, len(keys), 100):
        chunk = ", ".join(keys[i:i + 100])
        issues.extend(jira_client.search_issues(f"key in ({chunk})", fields=FIX_FIELDS))
    return issues


def _sync_fixes(conn, updated_clause):
    """SAL / SFS fix pool: Peacock-scoped SAL + SFS tickets, plus any SAL / SFS
    ticket a cached SAC ticket (or SFS ticket) links to."""
    for jql in config.TREND_FIX_JQLS:
        _upsert_fixes(conn, jira_client.search_issues(jql + updated_clause, fields=FIX_FIELDS))
    conn.commit()

    known = {r[0] for r in conn.execute("SELECT key FROM fixes")}
    wanted = set()
    for (links,) in conn.execute("SELECT links FROM tickets WHERE links != '[]'"):
        wanted.update(k for k in json.loads(links) if k.split("-")[0] in ("SAL", "SFS"))
    for (links,) in conn.execute("SELECT links FROM fixes WHERE project = 'SFS' AND links != '[]'"):
        wanted.update(k for k in json.loads(links) if k.startswith("SAL-"))
    missing = wanted - known
    if missing:
        # Linked tickets in projects we can't see raise 400s for the whole
        # chunk; fall back to one-by-one so the rest still load.
        try:
            _upsert_fixes(conn, _fetch_by_keys(missing))
        except Exception:
            for key in missing:
                try:
                    _upsert_fixes(conn, _fetch_by_keys([key]))
                except Exception:
                    pass
        conn.commit()


def sync(progress=None, force_full=False):
    """Brings the cache up to date. Returns a short status string."""
    started = datetime.now()
    conn = connect()
    try:
        last_sync = get_meta(conn, "last_sync")
        if force_full or not last_sync:
            _full_ticket_load(conn, progress)
            if progress:
                progress(1.0, "Loading SAL / SFS fix tickets")
            _sync_fixes(conn, "")
            mode = "full"
        else:
            minutes = int((started - datetime.fromisoformat(last_sync)).total_seconds() // 60) + 15
            clause = f" AND updated >= -{minutes}m"
            issues = jira_client.search_issues(config.TREND_TICKET_JQL + clause, fields=TICKET_FIELDS)
            _upsert_tickets(conn, issues)
            conn.commit()
            _sync_fixes(conn, clause)
            mode = f"incremental ({len(issues)} tickets updated)"

        cutoff = (date.today() - timedelta(days=config.TREND_HISTORY_DAYS + 7)).isoformat()
        conn.execute("DELETE FROM tickets WHERE created_date < ?", (cutoff,))
        set_meta(conn, "last_sync", started.isoformat(timespec="seconds"))
        conn.commit()
        return f"{mode} sync finished at {datetime.now():%Y-%m-%d %H:%M:%S}"
    finally:
        conn.close()


def last_sync_time():
    if not os.path.exists(DB_PATH):
        return None
    conn = connect()
    try:
        v = get_meta(conn, "last_sync")
        return datetime.fromisoformat(v) if v else None
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description="Sync the Ticket Trend Analysis cache from Jira.")
    parser.add_argument("--loop", action="store_true", help="keep syncing every TREND_SYNC_MINUTES")
    parser.add_argument("--full", action="store_true", help="reload the full history")
    args = parser.parse_args()

    def show(frac, msg):
        print(f"  [{frac:4.0%}] {msg}", flush=True)

    print(sync(show, force_full=args.full), flush=True)
    while args.loop:
        time.sleep(config.TREND_SYNC_MINUTES * 60)
        try:
            print(sync(show), flush=True)
        except Exception as exc:  # keep the agent alive through network blips
            print(f"Sync failed at {datetime.now():%Y-%m-%d %H:%M:%S}: {exc}", flush=True)


if __name__ == "__main__":
    main()
