"""
Local cache of Jira tickets for the Ticket Trend Analysis page.

Keeps the last TREND_HISTORY_DAYS of tickets for each trend category -- each
category's own JQL in config.TREND_CATEGORY_JQL, with which tickets matched
it recorded in the `membership` table -- plus the SAL / SFS fix tickets they
may relate to, in a SQLite file, so the trend page can slice months of data
by day / week / custom range without re-querying Jira.

    python trend_store.py           # one sync (full 1-year load on first run)
    python trend_store.py --loop    # sync agent: keeps the cache live, every
                                    # config.TREND_SYNC_MINUTES

The first run loads ~1 year (~75k tickets, about 10 minutes); every later run
only fetches tickets updated since the previous sync.
"""

import argparse
import json
import os
import sqlite3
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import breach_logic
import config
import jira_client

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "trend_data", "trend_cache.db")

TICKET_FIELDS = [
    "summary", "created", "resolutiondate", "status", "reporter", "issuelinks",
    config.TREND_FIELDS["reporting_area"], config.TREND_FIELDS["account_type"],
    config.TREND_FIELDS["incident_category"], config.TREND_FIELDS["incident_subcategory"],
    config.TREND_FIELDS["of_reason"], config.TREND_FIELDS["of_step"],
    config.TREND_FIELDS["of_type"], config.TREND_FIELDS["of_error"], config.SEVERITY_FIELD,
    config.TREND_FIELDS["sat_issue"], config.TREND_FIELDS["sat_subcategory"],
    config.TREND_FIELDS["sat_channel"], config.TREND_FIELDS["sat_order_type"],
    config.TREND_FIELDS["sat_customer_type"],
]
TICKET_COLUMNS = [
    "key", "created", "created_date", "resolved", "status", "reporter", "reporting_area",
    "account_type", "inc_cat", "inc_subcat", "of_step", "of_reason", "of_type", "of_error",
    "severity", "summary", "links", "sat_issue", "sat_subcat", "channel", "order_type", "customer_type",
]
# What the cached tickets were loaded with; a change triggers a full reload.
TICKET_SCOPE = json.dumps({"categories": config.TREND_CATEGORY_JQL, "fields": TICKET_FIELDS,
                           "history_days": config.TREND_HISTORY_DAYS}, sort_keys=True)
FIX_FIELDS = [
    "summary", "status", "created", "resolutiondate", "duedate", "fixVersions",
    "issuelinks", "labels", config.TREND_FIELDS["fix_eta"], config.TREND_FIELDS["fix_source"],
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS tickets (
    key TEXT PRIMARY KEY, created TEXT, created_date TEXT, resolved TEXT, status TEXT,
    reporter TEXT, reporting_area TEXT, account_type TEXT, inc_cat TEXT, inc_subcat TEXT,
    of_step TEXT, of_reason TEXT, of_type TEXT, of_error TEXT, severity TEXT,
    summary TEXT, links TEXT, sat_issue TEXT, sat_subcat TEXT, channel TEXT,
    order_type TEXT, customer_type TEXT
);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON tickets(created_date);
CREATE TABLE IF NOT EXISTS fixes (
    key TEXT PRIMARY KEY, project TEXT, summary TEXT, status TEXT, status_cat TEXT,
    created TEXT, resolved TEXT, duedate TEXT, eta TEXT, versions TEXT, links TEXT,
    labels TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS resolved (
    key TEXT, category TEXT, created TEXT, resolved TEXT, resolved_date TEXT, due TEXT,
    sla_status TEXT, severity TEXT, priority TEXT, status TEXT, assignee TEXT, summary TEXT,
    PRIMARY KEY (key, category)
);
CREATE INDEX IF NOT EXISTS idx_resolved_date ON resolved(resolved_date);
CREATE TABLE IF NOT EXISTS membership (key TEXT, category TEXT, PRIMARY KEY (key, category));
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")  # page reads while the sync agent writes
    conn.executescript(SCHEMA)
    # Caches created before a column existed get it added (filled on the next reload).
    have = {row[1] for row in conn.execute("PRAGMA table_info(tickets)")}
    for col in TICKET_COLUMNS:
        if col not in have:
            conn.execute(f"ALTER TABLE tickets ADD COLUMN {col} TEXT")
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
        _option(f.get(tf["sat_issue"])).strip(),
        _option(f.get(tf["sat_subcategory"])).strip(),
        _option(f.get(tf["sat_channel"])),
        _option(f.get(tf["sat_order_type"])),
        _option(f.get(tf["sat_customer_type"])),
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
    conn.executemany(
        f"INSERT OR REPLACE INTO tickets ({', '.join(TICKET_COLUMNS)}) "
        f"VALUES ({', '.join('?' * len(TICKET_COLUMNS))})",
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


# ---------------------------------------------------------------------------
# Resolved tickets (Resolved History page): the dashboard teams' resolved
# scopes, with SLA judged exactly like the dashboard's resolved cards.
# ---------------------------------------------------------------------------
RESOLVED_FIELDS = ["summary", "status", "assignee", "priority", "created", "resolutiondate", config.SEVERITY_FIELD]
RESOLVED_SCOPE = json.dumps({
    "categories": {c: cfg["resolved_scope_jql"] for c, cfg in config.CATEGORIES.items()},
    "sla": config.SLA_MINUTES, "fields": RESOLVED_FIELDS, "history_days": config.TREND_HISTORY_DAYS,
}, sort_keys=True)


def resolved_row(issue, category):
    f = issue["fields"]
    result = breach_logic.evaluate_resolution(issue, config.CATEGORIES[category])
    resolved = f.get("resolutiondate") or ""
    return (
        issue["key"], category, f.get("created") or "", resolved,
        resolved[:10],  # resolved day in the timezone Jira reported it in
        result["due_date"].isoformat() if result["due_date"] else "",
        result["status"], result["severity"] or "N/A",
        (f.get("priority") or {}).get("name", ""), (f.get("status") or {}).get("name", ""),
        (f.get("assignee") or {}).get("displayName", "Unassigned"), f.get("summary") or "",
    )


def _resolved_jql(category, extra):
    return f'({config.CATEGORIES[category]["resolved_scope_jql"]}) AND {extra}'


def _full_resolved_load(conn, progress=None):
    """TREND_HISTORY_DAYS of resolved tickets per dashboard team, in weekly
    slices of resolution date; swapped in at the end like membership."""
    start = breach_logic.jira_now().date() - timedelta(days=config.TREND_HISTORY_DAYS)
    tasks = [(cat, w) for cat in config.CATEGORIES for w in _weekly_windows(start, breach_logic.jira_now().date())]

    def fetch(task):
        cat, (a, b) = task
        jql = _resolved_jql(cat, f'resolutiondate >= "{a:%Y-%m-%d}" AND resolutiondate < "{b:%Y-%m-%d}"')
        return cat, jira_client.search_issues(jql, fields=RESOLVED_FIELDS)

    conn.execute("DROP TABLE IF EXISTS resolved_new")
    conn.execute("CREATE TABLE resolved_new AS SELECT * FROM resolved WHERE 0")
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for cat, issues in pool.map(fetch, tasks):
            conn.executemany("INSERT OR REPLACE INTO resolved_new VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                             [resolved_row(i, cat) for i in issues])
            conn.commit()
            done += 1
            if progress:
                progress(done / len(tasks), f"Loaded {done}/{len(tasks)} weekly slices of resolved tickets")
    conn.execute("DELETE FROM resolved")
    conn.execute("INSERT INTO resolved SELECT * FROM resolved_new")
    conn.execute("DROP TABLE resolved_new")
    conn.commit()


def _incremental_resolved_load(conn, clause, changed):
    """Re-checks every ticket changed since the last sync: still-resolved ones
    are refreshed, reopened ones drop out of the resolved history."""
    conn.executemany("DELETE FROM resolved WHERE key = ?", [(k,) for k in changed])
    for cat in config.CATEGORIES:
        issues = jira_client.search_issues(_resolved_jql(cat, clause), fields=RESOLVED_FIELDS)
        conn.executemany("INSERT OR REPLACE INTO resolved VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                         [resolved_row(i, cat) for i in issues])
    conn.commit()


def _category_jql(category, extra):
    return f"({config.TREND_CATEGORY_JQL[category]}) AND {extra}"


def _full_ticket_load(conn, progress=None):
    """Loads TREND_HISTORY_DAYS of tickets for every category, in weekly slices
    run several at a time. Membership is rebuilt in a side table and swapped
    in at the end, so the page keeps showing the old data while this runs."""
    start = breach_logic.jira_now().date() - timedelta(days=config.TREND_HISTORY_DAYS)
    tasks = [(cat, w) for cat in config.TREND_CATEGORY_JQL for w in _weekly_windows(start, breach_logic.jira_now().date())]

    def fetch(task):
        cat, (a, b) = task
        jql = _category_jql(cat, f'created >= "{a:%Y-%m-%d}" AND created < "{b:%Y-%m-%d}"')
        return cat, jira_client.search_issues(jql, fields=TICKET_FIELDS)

    conn.execute("DROP TABLE IF EXISTS membership_new")
    conn.execute("CREATE TABLE membership_new (key TEXT, category TEXT, PRIMARY KEY (key, category))")
    done = 0
    with ThreadPoolExecutor(max_workers=6) as pool:
        for cat, issues in pool.map(fetch, tasks):
            _upsert_tickets(conn, issues)
            conn.executemany("INSERT OR IGNORE INTO membership_new VALUES (?, ?)", [(i["key"], cat) for i in issues])
            conn.commit()
            done += 1
            if progress:
                progress(done / len(tasks), f"Loaded {done}/{len(tasks)} weekly slices of tickets")
    conn.execute("DELETE FROM membership")
    conn.execute("INSERT INTO membership SELECT key, category FROM membership_new")
    conn.execute("DROP TABLE membership_new")
    conn.commit()


def _changed_keys(clause):
    """Every SAC / SAT ticket updated in the window (Retail's project 10145 is SAC)."""
    return {i["key"] for i in jira_client.search_issues(config.TREND_TICKET_JQL + " AND " + clause, fields=["created"])}


def _incremental_ticket_load(conn, clause):
    """Tickets updated since the last sync: refreshes their fields and which
    categories they now match (a ticket can leave one, e.g. when cancelled)."""
    changed = _changed_keys(clause)
    matched = {}
    for cat in config.TREND_CATEGORY_JQL:
        issues = jira_client.search_issues(_category_jql(cat, clause), fields=TICKET_FIELDS)
        _upsert_tickets(conn, issues)
        matched[cat] = {i["key"] for i in issues}
        changed |= matched[cat]
    for cat, keys in matched.items():
        conn.executemany("DELETE FROM membership WHERE key = ? AND category = ?", [(k, cat) for k in changed])
        conn.executemany("INSERT OR IGNORE INTO membership VALUES (?, ?)", [(k, cat) for k in keys])
    conn.commit()
    return changed


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


# One sync at a time across every process (the sync agent, each dashboard
# server and the command line): overlapping full reloads overwrite each
# other's staging tables. A lock older than this is from a crashed run.
LOCK_PATH = os.path.join(os.path.dirname(DB_PATH), "sync.lock")
LOCK_STALE_MINUTES = 90


def _acquire_lock():
    os.makedirs(os.path.dirname(LOCK_PATH), exist_ok=True)
    for _ in range(2):
        try:
            fd = os.open(LOCK_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, f"pid {os.getpid()} at {datetime.now().isoformat(timespec='seconds')}".encode())
            os.close(fd)
            return True
        except FileExistsError:
            age = time.time() - os.path.getmtime(LOCK_PATH)
            if age < LOCK_STALE_MINUTES * 60:
                return False
            os.remove(LOCK_PATH)  # left behind by a crashed run
    return False


def sync_running():
    return os.path.exists(LOCK_PATH)


def sync(progress=None, force_full=False):
    """Brings the cache up to date, unless another process is already
    syncing. Returns a short status string."""
    if not _acquire_lock():
        return "skipped: another sync is already running"
    try:
        return _sync(progress, force_full)
    finally:
        os.remove(LOCK_PATH)


def _sync(progress, force_full):
    started = datetime.now()
    conn = connect()
    try:
        last_sync = get_meta(conn, "last_sync")
        if force_full or not last_sync or get_meta(conn, "ticket_scope") != TICKET_SCOPE:
            _full_ticket_load(conn, progress)
            _full_resolved_load(conn, progress)
            if progress:
                progress(1.0, "Loading SAL / SFS fix tickets")
            _sync_fixes(conn, "")
            mode = "full"
        else:
            minutes = int((started - datetime.fromisoformat(last_sync)).total_seconds() // 60) + 15
            clause = f"updated >= -{minutes}m"
            changed = _incremental_ticket_load(conn, clause)
            if get_meta(conn, "resolved_scope") == RESOLVED_SCOPE:
                _incremental_resolved_load(conn, clause, changed)
            else:
                _full_resolved_load(conn, progress)
            _sync_fixes(conn, " AND " + clause)
            mode = f"incremental ({len(changed)} tickets updated)"

        cutoff = (breach_logic.jira_now().date() - timedelta(days=config.TREND_HISTORY_DAYS + 7)).isoformat()
        conn.execute("DELETE FROM tickets WHERE created_date < ? OR key NOT IN (SELECT key FROM membership)", (cutoff,))
        conn.execute("DELETE FROM membership WHERE key NOT IN (SELECT key FROM tickets)")
        conn.execute("DELETE FROM resolved WHERE resolved_date < ?", (cutoff,))
        set_meta(conn, "last_sync", started.isoformat(timespec="seconds"))
        set_meta(conn, "ticket_scope", TICKET_SCOPE)
        set_meta(conn, "resolved_scope", RESOLVED_SCOPE)
        conn.commit()
        return f"{mode} sync finished at {datetime.now():%Y-%m-%d %H:%M:%S}"
    finally:
        conn.close()


def needs_full_reload():
    """True when the cache was loaded with a different ticket scope / field list."""
    if not os.path.exists(DB_PATH):
        return True
    conn = connect()
    try:
        return (get_meta(conn, "ticket_scope") != TICKET_SCOPE
                or get_meta(conn, "resolved_scope") != RESOLVED_SCOPE)
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
