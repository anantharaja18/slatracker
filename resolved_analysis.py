"""
Resolved Tickets History logic (no Streamlit): the dashboard teams' resolved
tickets from the local cache (trend_store's `resolved` table), summarised per
team, per day / week and per assignee for any period.

SLA is judged exactly like the dashboard's resolved cards: resolved by
Created + config.SLA_MINUTES[severity] = within SLA, after it = resolved
late, no SLA for the severity = not judged.
"""

from datetime import timedelta

import pandas as pd

import breach_logic
import config
import jira_client
import trend_analysis as ta
import trend_store

TEAMS = list(config.CATEGORIES)
WITHIN, LATE, NOT_JUDGED = "Within SLA", "Resolved late", "No SLA (severity not set)"
SLA_LABEL = {breach_logic.STATUS_OK: WITHIN, breach_logic.STATUS_BREACHED: LATE,
             breach_logic.STATUS_UNKNOWN: NOT_JUDGED}


def load_resolved():
    conn = trend_store.connect()
    try:
        df = pd.read_sql_query("SELECT * FROM resolved", conn)
    finally:
        conn.close()
    if df.empty:
        return df
    df["resolved_date"] = pd.to_datetime(df["resolved_date"]).dt.date
    created = pd.to_datetime(df["created"], utc=True, format="ISO8601")
    resolved = pd.to_datetime(df["resolved"], utc=True, format="ISO8601")
    df["hours_to_resolve"] = ((resolved - created).dt.total_seconds() / 3600).round(1)
    df["sla"] = df["sla_status"].map(SLA_LABEL).fillna(NOT_JUDGED)
    df["same_day"] = df["created"].str[:10] == df["resolved"].str[:10]
    return df


def in_period(df, start, end):
    return df[(df["resolved_date"] >= start) & (df["resolved_date"] <= end)]


# Which resolved tickets to count, like the dashboard's header: created on the
# day they were resolved (that day's inflow) or created on an earlier day.
INFLOW_ALL, INFLOW_SAME_DAY, INFLOW_PAST = "All resolved", "Resolved from the day's inflow", "Resolved from past inflow"
INFLOW_OPTIONS = [INFLOW_ALL, INFLOW_SAME_DAY, INFLOW_PAST]


def by_inflow(df, which):
    if which == INFLOW_SAME_DAY:
        return df[df["same_day"]]
    if which == INFLOW_PAST:
        return df[~df["same_day"]]
    return df


def sla_pct(within, late):
    judged = within + late
    return round(within / judged * 100, 1) if judged else None


def _counts(part):
    within = int((part["sla"] == WITHIN).sum())
    late = int((part["sla"] == LATE).sum())
    return {
        "resolved": len(part), "within": within, "late": late,
        "not_judged": len(part) - within - late, "pct": sla_pct(within, late),
        "median_hours": float(part["hours_to_resolve"].median()) if len(part) else None,
        "same_day": int(part["same_day"].sum()),
    }


def team_summary(df, start, end, prev_range):
    """Per team (and "All teams", each ticket once) for the period and the
    comparison period."""
    cur, prev = in_period(df, start, end), in_period(df, *prev_range)
    out = {}
    for team in TEAMS:
        out[team] = {**_counts(cur[cur["category"] == team]),
                     "prev": _counts(prev[prev["category"] == team])}
    out["All teams"] = {**_counts(cur.drop_duplicates("key")), "prev": _counts(prev.drop_duplicates("key"))}
    return out


def sla_by_severity(df, start, end):
    """Per team and per severity (the keys of config.SLA_MINUTES, e.g. Sev-1 and
    Sev-2): resolved, within SLA, resolved late and SLA %. "All teams" counts
    each ticket once. Tickets with no matching severity are not in these columns."""
    cur = in_period(df, start, end)
    rows = []
    for team in [*TEAMS, "All teams"]:
        part = cur.drop_duplicates("key") if team == "All teams" else cur[cur["category"] == team]
        row = {"Team": team}
        for sev in config.SLA_MINUTES:
            c = _counts(part[part["severity"] == sev])
            row.update({f"{sev} Resolved": c["resolved"], f"{sev} Within SLA": c["within"],
                        f"{sev} Resolved late": c["late"], f"{sev} SLA %": c["pct"]})
        rows.append(row)
    return pd.DataFrame(rows)


OPEN_FIELDS = ["summary", "status", "created", config.SEVERITY_FIELD, breach_logic.JIRA_SLA_FIELD,
               breach_logic.ESCALATION_PATH_FIELD]


def load_open_breached():
    """Open tickets of each team that are breached right now, from Jira. One row per
    ticket and team: key, category, created (YYYY-MM-DD, Jira time), severity, breached.
    A breach is left out for Escalated tickets on the Network / A2P path."""
    rows = []
    for team, cfg in config.CATEGORIES.items():
        jqls = [cfg["jql"]] + ([cfg["backlog_jql"]] if cfg.get("backlog_jql") else [])
        seen = set()
        for jql in jqls:
            for issue in jira_client.search_issues(jql, fields=OPEN_FIELDS):
                if issue["key"] in seen:
                    continue
                seen.add(issue["key"])
                f = issue["fields"]
                result = breach_logic.evaluate(issue, cfg)
                path = f.get(breach_logic.ESCALATION_PATH_FIELD)
                path = path.get("value") if isinstance(path, dict) else path
                breached = (result["status"] == breach_logic.STATUS_BREACHED and not breach_logic.is_excluded_open_breached(
                    (f.get("status") or {}).get("name"), path))
                rows.append({"key": issue["key"], "category": team, "created": (f.get("created") or "")[:10],
                             "severity": result["severity"] or "N/A", "breached": breached,
                             "status": (f.get("status") or {}).get("name", ""),
                             "assignee": (f.get("assignee") or {}).get("displayName", "Unassigned"),
                             "escalation": path or "",
                             "summary": f.get("summary") or ""})
    return pd.DataFrame(rows, columns=["key", "category", "created", "severity", "breached", "status",
                                       "assignee", "summary", "escalation"])


def sla_by_created(df, open_rows, start, end):
    """SLA by severity for tickets CREATED in [start, end], wherever they stand now.
    Resolved ones are judged on their resolution (within / resolved late, as the
    dashboard). Open ones count as Open breached only if they are breached now.
    Open tickets not yet breached are not judged. "All teams" counts each ticket once."""
    lo, hi = f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"
    res = df[(df["created"].str[:10] >= lo) & (df["created"].str[:10] <= hi)]
    opn = open_rows[(open_rows["created"] >= lo) & (open_rows["created"] <= hi) & open_rows["breached"]]
    rows = []
    for team in [*TEAMS, "All teams"]:
        r_part = res.drop_duplicates("key") if team == "All teams" else res[res["category"] == team]
        o_part = opn.drop_duplicates("key") if team == "All teams" else opn[opn["category"] == team]
        row = {"Team": team}
        for sev in config.SLA_MINUTES:
            rs = r_part[r_part["severity"] == sev]
            within = int((rs["sla"] == WITHIN).sum())
            late = int((rs["sla"] == LATE).sum())
            open_n = int((o_part["severity"] == sev).sum())
            judged = within + late + open_n
            row.update({f"{sev} Resolved": len(rs), f"{sev} Within SLA": within,
                        f"{sev} Resolved late": late, f"{sev} Open breached": open_n,
                        f"{sev} SLA %": round(within / judged * 100, 1) if judged else None})
        rows.append(row)
    return pd.DataFrame(rows)


def _created_counts(df, open_rows, start, end, team):
    lo, hi = f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"
    res = df[(df["created"].str[:10] >= lo) & (df["created"].str[:10] <= hi)]
    opn = open_rows[(open_rows["created"] >= lo) & (open_rows["created"] <= hi) & open_rows["breached"]]
    if team == "All teams":
        res, opn = res.drop_duplicates("key"), opn.drop_duplicates("key")
    else:
        res, opn = res[res["category"] == team], opn[opn["category"] == team]
    within = int((res["sla"] == WITHIN).sum())
    late = int((res["sla"] == LATE).sum())
    open_n = len(opn)
    breached = late + open_n
    return {"resolved": len(res), "within": within, "late": late, "open_breached": open_n,
            "breached": breached, "pct": sla_pct(within, breached)}


def created_summary(df, open_rows, start, end, prev_range):
    """Per team (and "All teams", each ticket once) for tickets CREATED in the period,
    with Within SLA and Breached SLA (= resolved late + open breached), and the
    same for the comparison period."""
    out = {}
    for team in [*TEAMS, "All teams"]:
        out[team] = {**_created_counts(df, open_rows, start, end, team),
                     "prev": _created_counts(df, open_rows, *prev_range, team)}
    return out


def breached_tickets(df, open_rows, start, end, team, sev):
    """The tickets behind one Breached SLA count (team and severity, created in
    [start, end]): resolved late, then open breached. Returns a list of dicts."""
    lo, hi = f"{start:%Y-%m-%d}", f"{end:%Y-%m-%d}"
    res = df[(df["created"].str[:10] >= lo) & (df["created"].str[:10] <= hi)
             & (df["sla"] == LATE) & (df["severity"] == sev)]
    opn = open_rows[(open_rows["created"] >= lo) & (open_rows["created"] <= hi)
                    & open_rows["breached"] & (open_rows["severity"] == sev)]
    if team == "All teams":
        res, opn = res.drop_duplicates("key"), opn.drop_duplicates("key")
    else:
        res, opn = res[res["category"] == team], opn[opn["category"] == team]
    out = []
    for part in (res, opn):
        for _, row in part.iterrows():
            out.append({"key": row["key"], "team": row["category"], "assignee": row["assignee"] or "Unassigned",
                        "severity": row["severity"] or "N/A", "status": row["status"] or "",
                        "summary": row["summary"] or "", "due": "",
                        "escalation": row["escalation"] if "escalation" in row.index and isinstance(row["escalation"], str) else ""})
    return out


def trend(df, first, last, by):
    """Resolved / within / late / SLA % per team per day or per Sunday-Saturday
    week, for every bucket in [first, last] (empty ones included)."""
    part = in_period(df, first, last).copy()
    if by == "week":
        part["bucket"] = part["resolved_date"].map(ta.week_start)
        buckets, b = [], ta.week_start(first)
        while b <= last:
            buckets.append(b)
            b += timedelta(weeks=1)
    else:
        part["bucket"] = part["resolved_date"]
        buckets = [first + timedelta(days=i) for i in range((last - first).days + 1)]
    grid = pd.MultiIndex.from_product([buckets, TEAMS], names=["bucket", "category"])
    g = part.groupby(["bucket", "category"])
    out = pd.DataFrame({
        "resolved": g.size(),
        "within": g["sla"].apply(lambda s: int((s == WITHIN).sum())),
        "late": g["sla"].apply(lambda s: int((s == LATE).sum())),
    }).reindex(grid, fill_value=0).reset_index()
    out["sla_pct"] = [sla_pct(w, l) for w, l in zip(out["within"], out["late"])]
    out["label"] = out["bucket"].map(ta.week_label if by == "week" else (lambda d: f"{d:%a %d %b %Y}"))
    return out


def assignees_in(df, start, end, teams):
    """Everyone who resolved a ticket for these teams in the period, A-Z."""
    part = in_period(df, start, end)
    return sorted(part.loc[part["category"].isin(teams), "assignee"].unique())


def by_assignee(df, start, end, teams, assignees=None):
    """Per-assignee resolved counts per team, plus SLA totals, busiest first,
    with a Grand Total row (of the assignees shown). No assignees = everyone."""
    part = in_period(df, start, end)
    part = part[part["category"].isin(teams)]
    if assignees:
        part = part[part["assignee"].isin(assignees)]
    if part.empty:
        return pd.DataFrame()
    counts = part.pivot_table(index="assignee", columns="category", values="key", aggfunc="count", fill_value=0)
    counts = counts.reindex(columns=[t for t in teams if t in counts.columns]).add_suffix(" (Resolved)")
    within = part[part["sla"] == WITHIN].groupby("assignee").size()
    late = part[part["sla"] == LATE].groupby("assignee").size()
    same_day = part[part["same_day"]].groupby("assignee").size().reindex(counts.index, fill_value=0)
    table = counts.assign(**{
        "Total Resolved": counts.sum(axis=1),
        INFLOW_SAME_DAY: same_day,
        INFLOW_PAST: counts.sum(axis=1) - same_day,
        "Within SLA": within.reindex(counts.index, fill_value=0),
        "Resolved late": late.reindex(counts.index, fill_value=0),
    })
    table["SLA %"] = [sla_pct(w, l) for w, l in zip(table["Within SLA"], table["Resolved late"])]
    table = table.sort_values("Total Resolved", ascending=False).reset_index().rename(columns={"assignee": "Assignee"})
    total = table.drop(columns=["Assignee", "SLA %"]).sum()
    total_row = {"Assignee": "Grand Total", **total.to_dict(),
                 "SLA %": sla_pct(total["Within SLA"], total["Resolved late"])}
    return pd.concat([table, pd.DataFrame([total_row])], ignore_index=True)


def ticket_list(df, start, end):
    part = in_period(df, start, end).sort_values("resolved", ascending=False)
    return pd.DataFrame({
        "S.No": range(1, len(part) + 1),
        "Key": part["key"].values,
        "Team": part["category"].values,
        "Summary": part["summary"].values,
        "Severity": part["severity"].values,
        "Priority": part["priority"].values,
        "Status": part["status"].values,
        "Assignee": part["assignee"].values,
        "Created": part["created"].str[:16].str.replace("T", " ").values,
        "Resolved": part["resolved"].str[:16].str.replace("T", " ").values,
        "Hours to Resolve": part["hours_to_resolve"].values,
        "SLA Due": part["due"].str[:16].str.replace("T", " ").values,
        "SLA": part["sla"].values,
        "Created & Resolved Same Day": part["same_day"].map({True: "Yes", False: "No"}).values,
    })
