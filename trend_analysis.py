"""
Ticket Trend Analysis logic (no Streamlit): puts each cached ticket in a
category and a "driver" (the kind of issue it is), ranks the top drivers for
a period, builds weekly (Sunday-Saturday) trends, and matches each driver to
SAL (development fix) / SFS (Salesforce support) tickets -- flagging fixes
that were deployed while the issue keeps coming in.
"""

import json
import re
from collections import Counter
from datetime import date, datetime, timedelta

import pandas as pd

import config
import trend_store

CATEGORIES = ["SAC", "B2B", "Order Fallout"]
UNCATEGORISED = "Uncategorised"

# SAC / B2B tickets with no usable Incident Sub Category get a theme from their
# summary (Retail agents rarely fill the category). First matching rule wins.
SUMMARY_THEMES = [
    ("TOBR / transfer of ownership", r"\btobr\b|transfer of (owner|responsib)"),
    ("Paid but line still suspended", r"(pag|paid|payment|pay).*(suspend)|(suspend).*(pag|paid|payment)"),
    ("Resume / reactivate line", r"resum|restor|reactiv|reconnect|reconex"),
    ("Port-in / port-out", r"port[ -]?(in|out)\b|portab|\bport\b"),
    ("Device upgrade failure", r"upgrad"),
    ("Insurance / add-on", r"insurance|asurion|seguro|add[ -]?on"),
    ("Plan change", r"change plan|plan change|cambio de plan|u ?pick|\bplan\b"),
    ("Wearable / eSIM / SIM", r"wearable|watch|e-?sim\b|\bsim\b|iccid"),
    ("Activation / new line", r"activat|activar|new line|nueva linea|linea nueva"),
    ("Payment / wallet / billing", r"wallet|balance|payment|pago|cobr|autopay|charg|bill|factur"),
    ("No service / calls / data", r"no service|sin servicio|\bcall|llamad|\bdata\b|signal|\bots\b"),
    ("ARIA sync / data mismatch", r"\baria\b|sync|mismatch|discrepan"),
    ("Cancel line / service", r"cancel"),
    ("Suspend / disconnect", r"suspen|disconnect|desconect"),
    ("Order failed / stuck", r"order|orden|orchestrat|fatal|fail|stuck|nokia|matrix"),
]
_THEMES = [(name, re.compile(rx, re.I)) for name, rx in SUMMARY_THEMES]
UNUSABLE_SUBCATEGORIES = {"", "new category needed", "other", "others", "n/a", "na"}

# Order Fallout errors carry ids, numbers and urls that differ per order;
# masking them groups the same error together.
_URL = re.compile(r"https?://\s*([^/\s']+)[^\s']*")
_SFID = re.compile(r"\b(?=[A-Za-z0-9]*\d)(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{15,18}\b")
_NUM = re.compile(r"\b\d{5,}\b")
_JUNK = re.compile(r"[^\w\s.,:;'\"()/\-|!?#&%+=\[\]{}@<>]")
_SPACE = re.compile(r"\s+")


def normalize_error(text):
    text = _URL.sub(r"\1", text or "")
    text = _SFID.sub("<id>", text)
    text = _NUM.sub("<n>", text)
    text = _JUNK.sub("", text)
    text = _SPACE.sub(" ", text).strip(" -|!")
    return text[:140] or "(no error description)"


def summary_theme(summary):
    s = re.sub(r"^\s*possible duplicate\s*-\s*", "", summary or "", flags=re.I)
    for name, rx in _THEMES:
        if rx.search(s):
            return name
    return UNCATEGORISED


def _driver_columns(df):
    """Each ticket's trend category, driver and how the driver was derived.

    Category: Order Fallout (Reporting Area), then B2B (Account type), then SAC
    -- the Retail + SAC dashboard scopes (non-B2B, not raised by the SAC BOT
    unless Retail). Driver: Order Fallout = failed step + normalised error;
    SAC / B2B = Incident Sub Category, else a theme from the summary. Every
    distinct error / summary is normalised once to keep a 6-month load fast."""
    area = df["reporting_area"].fillna("").str.strip().str.lower()
    b2b = df["account_type"].fillna("").str.strip().str.upper() == "B2B"
    category = pd.Series(None, index=df.index, dtype=object)
    category[(area == "retail") | (df["reporter"] != "SAC BOT")] = "SAC"
    category[b2b] = "B2B"
    category[area == "order fallout"] = "Order Fallout"

    errors = df["of_error"].fillna("")
    err_norm = errors.map({e: normalize_error(e) for e in errors.unique()})
    of_driver = df["of_step"].fillna("").replace("", "(no failed step)") + " | " + err_norm

    sub = df["inc_subcat"].fillna("").str.strip()
    usable = ~sub.str.lower().isin(UNUSABLE_SUBCATEGORIES)
    summaries = df["summary"].fillna("")
    themes = summaries.map({x: summary_theme(x) for x in summaries.unique()})

    is_of = category == "Order Fallout"
    driver = of_driver.where(is_of, sub.where(usable, themes))
    source = pd.Series("Summary theme", index=df.index)
    source[usable] = "Incident Sub Category"
    source[is_of] = "Failed step + error"
    return category, driver, source


def load_tickets():
    conn = trend_store.connect()
    try:
        df = pd.read_sql_query("SELECT * FROM tickets", conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["category"], df["driver"], df["driver_source"] = _driver_columns(df)
    df = df[df["category"].notna()].copy()
    df["created_date"] = pd.to_datetime(df["created_date"]).dt.date
    df["links"] = df["links"].map(json.loads)
    area = df["reporting_area"].fillna("").str.lower()
    df["sub_team"] = df["account_type"].fillna("").replace("", "—")
    df.loc[df["category"] == "SAC", "sub_team"] = area.map(lambda a: "Retail" if a == "retail" else "SAC")
    df["is_open"] = df["resolved"].fillna("") == ""
    return df


def jira_today(df):
    """Today in the timezone Jira reports timestamps in (the cache stores
    created dates in that timezone), not the PC's own date."""
    if df.empty:
        return date.today()
    latest = datetime.fromisoformat(df["created"].max())
    return datetime.now(latest.tzinfo).date() if latest.tzinfo else date.today()


def load_fixes():
    conn = trend_store.connect()
    try:
        df = pd.read_sql_query("SELECT * FROM fixes", conn)
    finally:
        conn.close()
    for col in ("versions", "links", "labels"):
        df[col] = df[col].map(json.loads) if not df.empty else df[col]
    return df


# ---------------------------------------------------------------------------
# Periods
# ---------------------------------------------------------------------------
def week_start(d):
    """Sunday on or before d (weeks run Sunday - Saturday)."""
    return d - timedelta(days=(d.weekday() + 1) % 7)


def week_label(start):
    end = start + timedelta(days=6)
    return f"{start:%d %b} – {end:%d %b %Y}"


def previous_period(start, end, week=False):
    """The comparison period: the same days of the previous week for a week
    (so a week in progress is compared like for like), otherwise the
    equally long period just before."""
    if week:
        return start - timedelta(days=7), end - timedelta(days=7)
    length = (end - start).days + 1
    return start - timedelta(days=length), start - timedelta(days=1)


def in_period(df, start, end):
    return df[(df["created_date"] >= start) & (df["created_date"] <= end)]


# ---------------------------------------------------------------------------
# Top drivers
# ---------------------------------------------------------------------------
def top_drivers(df, category, start, end, prev_range, n=config.TREND_TOP_N):
    """Top n drivers of a category in [start, end], with the count in
    prev_range for comparison. Uncategorised tickets are counted but never ranked."""
    cat = df[df["category"] == category]
    cur = in_period(cat, start, end)
    prev = in_period(cat, *prev_range)
    counts = cur[cur["driver"] != UNCATEGORISED]["driver"].value_counts()
    prev_counts = prev["driver"].value_counts()
    total = len(cur)
    rows = []
    for rank, (driver, count) in enumerate(counts.head(n).items(), start=1):
        rows.append({
            "rank": rank, "driver": driver, "count": int(count),
            "prev": int(prev_counts.get(driver, 0)),
            "share": round(count / total * 100, 1) if total else 0.0,
        })
    return {
        "total": total, "prev_total": len(prev),
        "uncategorised": int((cur["driver"] == UNCATEGORISED).sum()),
        "drivers": rows,
    }


def weekly_trend(df, category, weeks, end_day, n=config.TREND_TOP_N):
    """Weekly counts (Sunday-Saturday) of the category's top n drivers over the
    last `weeks` weeks ending with end_day's week; other drivers fold into Other."""
    last = week_start(end_day)
    first = last - timedelta(weeks=weeks - 1)
    cat = df[(df["category"] == category) & (df["created_date"] >= first)
             & (df["created_date"] <= last + timedelta(days=6))].copy()
    if cat.empty:
        return pd.DataFrame(columns=["week", "week_label", "driver", "count"]), []
    cat["week"] = cat["created_date"].map(week_start)
    top = cat[cat["driver"] != UNCATEGORISED]["driver"].value_counts().head(n).index.tolist()
    cat["series"] = cat["driver"].where(cat["driver"].isin(top), "Other")
    grid = pd.MultiIndex.from_product(
        [[first + timedelta(weeks=i) for i in range(weeks)], top + ["Other"]], names=["week", "driver"])
    out = cat.groupby(["week", "series"]).size().reindex(grid, fill_value=0).reset_index(name="count")
    out["week_label"] = out["week"].map(week_label)
    return out, top


# ---------------------------------------------------------------------------
# Issue summary
# ---------------------------------------------------------------------------
def _top_values(series, k=3):
    vals = [v for v in series if v]
    return Counter(vals).most_common(k)


def driver_summary(df, category, driver, start, end):
    """Plain-language summary of one driver in the period, plus its tickets."""
    all_t = df[(df["category"] == category) & (df["driver"] == driver)]
    cur = in_period(all_t, start, end).sort_values("created", ascending=False)
    first_seen = all_t["created_date"].min()
    lines = []
    split = ", ".join(f"{k} {v}" for k, v in _top_values(cur["sub_team"], 4))
    open_n = int(cur["is_open"].sum())
    lines.append(f"{len(cur)} ticket(s) in the period ({split}); {open_n} still open, "
                 f"{len(cur) - open_n} resolved.")
    if category == "Order Fallout":
        reasons = ", ".join(f"{k} ({v})" for k, v in _top_values(cur["of_reason"]))
        types = ", ".join(f"{k} ({v})" for k, v in _top_values(cur["of_type"]))
        if reasons:
            lines.append(f"Order Fallout Reason: {reasons}.")
        if types:
            lines.append(f"Order type: {types}.")
        raw = _top_values(cur["of_error"], 1)
        if raw:
            lines.append(f"Example error: \"{raw[0][0][:220]}\"")
    else:
        if driver_source_of(all_t) == "Summary theme":
            lines.append("Grouped by summary keywords (no Incident Sub Category set on these tickets).")
        cats = ", ".join(f"{k} ({v})" for k, v in _top_values(cur["inc_cat"]))
        if cats:
            lines.append(f"Incident Category: {cats}.")
        examples = "; ".join(f"\"{s[:90]}\"" for s, _ in _top_values(cur["summary"], 3))
        if examples:
            lines.append(f"Common summaries: {examples}.")
    sev = ", ".join(f"{k} ({v})" for k, v in _top_values(cur["severity"], 4))
    if sev:
        lines.append(f"Severity: {sev}.")
    if pd.notna(first_seen):
        lines.append(f"Seen {len(all_t)} time(s) in the last 6 months, first on {first_seen:%d %b %Y}.")
    return {"text": " ".join(lines), "tickets": cur, "all_tickets": all_t}


def driver_source_of(tickets):
    return tickets["driver_source"].mode().iat[0] if not tickets.empty else ""


# ---------------------------------------------------------------------------
# SAL / SFS fix matching
# ---------------------------------------------------------------------------
_STOP = set("""a an the and or of to in on for by with from is are be not no it its at as this that
was were has have had into via de la el en y del los las por para con una un es se b2c b2b
order orders error issue issues line lines ticket fallout step failed failure""".split())
_TOKEN = re.compile(r"[a-z0-9]+")


def _tokens(text):
    return {t for t in _TOKEN.findall((text or "").lower()) if t not in _STOP and len(t) > 1 and not t.isdigit()}


def _norm(text):
    return " ".join(_TOKEN.findall((text or "").lower()))


# Summary matches at or above this score are treated as the same issue (and can
# raise "still occurring after fix"); weaker ones are shown as possibly related.
STRONG_MATCH = 0.8
RECURRING_LOOKBACK_DAYS = 30


def text_match_score(category, driver, fix_summary):
    """0-1 likelihood that a SAL / SFS summary describes this driver's issue."""
    fix_norm = _norm(fix_summary)
    fix_tokens = _tokens(fix_summary)
    if category == "Order Fallout":
        step, _, err = driver.partition(" | ")
        step_norm = _norm(step)
        err_tokens = _tokens(err.replace("<id>", " ").replace("<n>", " "))
        err_cov = len(err_tokens & fix_tokens) / len(err_tokens) if err_tokens else 0
        step_hit = bool(step_norm) and len(step_norm) >= 6 and step_norm in fix_norm
        err_hit = len(err_tokens) >= 2 and err_cov >= 0.8
        if step_hit and err_hit:
            return 1.0
        if err_hit:
            return 0.8
        if step_hit:
            return 0.6  # same failed step, maybe a different error
        return 0.0
    tokens = _tokens(driver)
    if len(tokens) >= 2 and tokens <= fix_tokens:
        return 0.5
    return 0.0


def _parse_day(text):
    try:
        return date.fromisoformat((text or "")[:10])
    except ValueError:
        return None


def fix_state(fix, today=None):
    """Where a SAL / SFS ticket stands: state, the relevant date and a label."""
    today = today or date.today()
    status = (fix["status"] or "").strip()
    s = status.lower()
    versions = fix["versions"] or []
    released = [_parse_day(v["releaseDate"]) for v in versions if v.get("released") and v.get("releaseDate")]
    unreleased = [_parse_day(v["releaseDate"]) for v in versions if not v.get("released") and v.get("releaseDate")]
    planned = min([d for d in unreleased if d] or [None], key=lambda d: d or date.max) \
        or _parse_day(fix["eta"]) or _parse_day(fix["duedate"])

    if fix["project"] == "SFS":
        if fix["status_cat"] == "done":
            if s == "cancelled":
                return {"state": "cancelled", "date": None, "label": "Cancelled"}
            return {"state": "resolved", "date": _parse_day(fix["resolved"]), "label": f"{status}"}
        return {"state": "open", "date": None, "label": f"Open ({status})"}

    if s == "cancelled":
        return {"state": "cancelled", "date": None, "label": "Cancelled"}
    if s == "waiting for release":
        return {"state": "awaiting_release", "date": planned, "label": "Fix ready, waiting for release"}
    if fix["status_cat"] == "done" and s not in ("po approval", "approved"):
        deployed = max([d for d in released if d] or [None], key=lambda d: d or date.min) \
            or _parse_day(fix["eta"]) or _parse_day(fix["resolved"])
        return {"state": "deployed", "date": deployed, "label": "Deployed"}
    if fix["status_cat"] == "new":
        return {"state": "not_started", "date": planned, "label": f"Not started ({status})"}
    return {"state": "in_progress", "date": planned, "label": f"In progress ({status})"}


def match_fixes(df, fixes, category, driver, period_end, max_per_project=3):
    """SAL / SFS tickets for a driver: directly linked ones first, ranked by how
    many of the driver's tickets link to them (an SFS's own SAL links are
    followed too), then summary-text matches. Each comes with its state and
    any alert."""
    if fixes.empty:
        return []
    all_t = df[(df["category"] == category) & (df["driver"] == driver)
               & (df["created_date"] <= period_end)]
    by_key = fixes.set_index("key", drop=False)

    link_counts = Counter(k for links in all_t["links"] for k in links if k.split("-")[0] in ("SAL", "SFS"))
    via_sfs = {}
    for key in list(link_counts):
        if key.startswith("SFS-") and key in by_key.index:
            for k in by_key.at[key, "links"]:
                if k.startswith("SAL-") and k not in link_counts:
                    via_sfs[k] = key

    candidates = {}
    for key, n in link_counts.items():
        if key in by_key.index:
            candidates[key] = {"match": "Linked", "score": 1.0, "linked_tickets": n, "via": ""}
    for key, sfs in via_sfs.items():
        if key in by_key.index:
            candidates[key] = {"match": "Linked", "score": 0.9, "linked_tickets": 0, "via": sfs}
    for fix in fixes.itertuples(index=False):
        if fix.key in candidates:
            continue
        score = text_match_score(category, driver, fix.summary)
        if score >= 0.5:
            candidates[fix.key] = {"match": "Summary match", "score": round(score, 2), "linked_tickets": 0, "via": ""}

    results = []
    for key, info in candidates.items():
        fix = by_key.loc[key]
        state = fix_state(fix, today=period_end)
        if state["state"] == "cancelled" and info["match"] != "Linked":
            continue
        # Recurrence: a fix whose summary strongly matches describes the whole
        # driver, so every later driver ticket counts; a fix that is only
        # linked counts later tickets linked to that same fix (or its SFS).
        if info["match"] == "Linked" and text_match_score(category, driver, fix["summary"]) < STRONG_MATCH:
            ref = info["via"] or key
            related = all_t[all_t["links"].map(lambda links: ref in links)]
        elif info["match"] == "Linked" or info["score"] >= STRONG_MATCH:
            related = all_t
        else:
            info["match"] = "Possibly related"
            related = all_t.iloc[0:0]  # too loose a match to call the issue recurring
        results.append({**info, "key": key, "project": fix["project"], "summary": fix["summary"],
                        "status": fix["status"], **state, **recurrence_alert(related, state, period_end)})
    results.sort(key=lambda r: (r["match"] != "Linked", -r["score"], -r["linked_tickets"],
                                r["state"] == "cancelled", -(r["date"] or date.min).toordinal()))
    out, per_project, loose = [], Counter(), Counter()
    for r in results:
        if per_project[r["project"]] >= max_per_project:
            continue
        if r["match"] == "Possibly related":
            if loose[r["project"]]:  # one weak suggestion per project is enough
                continue
            loose[r["project"]] += 1
        out.append(r)
        per_project[r["project"]] += 1
    return out


def recurrence_alert(related_tickets, state, today=None):
    """Deployed / resolved fix but tickets still arriving after it, or a
    planned date that has passed without deployment."""
    today = today or date.today()
    if state["state"] in ("deployed", "resolved") and state["date"]:
        after = related_tickets[related_tickets["created_date"] > state["date"]]
        # Only "still occurring" if a post-fix ticket arrived recently.
        if len(after) and after["created_date"].max() >= today - timedelta(days=RECURRING_LOOKBACK_DAYS):
            what = "Fix deployed" if state["state"] == "deployed" else "SFS resolved"
            return {"alert": "recurring",
                    "alert_text": f"{what} on {state['date']:%d %b %Y} but {len(after)} new ticket(s) since "
                                  f"(latest {after['created_date'].max():%d %b %Y})",
                    "since_count": len(after)}
    if state["state"] in ("in_progress", "not_started", "awaiting_release") and state["date"] and state["date"] < today:
        return {"alert": "overdue", "alert_text": f"Planned date {state['date']:%d %b %Y} has passed, not deployed yet",
                "since_count": 0}
    return {"alert": None, "alert_text": "", "since_count": 0}


def fix_status_text(fixes_for_driver, project):
    items = [f for f in fixes_for_driver if f["project"] == project]
    if not items:
        return f"No {project} ticket found"
    parts = []
    for f in items:
        when = f" ({'deployed' if f['state'] == 'deployed' else 'resolved' if f['state'] == 'resolved' else 'planned'} "\
               f"{f['date']:%d %b %Y})" if f["date"] else ""
        parts.append(f"{f['key']} – {f['label']}{when} [{f['match']}]")
    return "; ".join(parts)
