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
    table = counts.assign(**{
        "Total Resolved": counts.sum(axis=1),
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
