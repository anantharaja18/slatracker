"""
Chat assistant embedded in the dashboard: lets the user ask free-text
questions ("what's breaching in B2B right now?") answered by Claude via
tool use over the same category_data / resolved_data already loaded for
the dashboard's own sections -- no extra Jira calls per question.

This is a manual agentic tool-use loop (see the Anthropic docs on tool
use): Claude decides which of the tools below to call, we run the plain
Python function against the in-memory ticket data and hand the result
back, and Claude turns that into a natural-language answer.
"""

import json
from collections import defaultdict

import anthropic

import breach_logic
import config

SYSTEM_PROMPT = (
    "You are an assistant embedded in a Jira SLA Tracker dashboard covering "
    "four ticket queues: Retail, SAC, B2B, and Order Fallout. Answer "
    "questions about SLA breach status, open ticket counts, and "
    "productivity using the tools provided -- they return a live snapshot "
    "of the data, so never guess or invent ticket numbers, keys, or counts. "
    "If a question doesn't name a specific team, query all of them. Be "
    "concise: a short paragraph or a compact bullet list, not a wall of "
    "text. When you list tickets, include their key so the user can find "
    "them in the dashboard."
)

CATEGORY_ENUM = ["Retail", "SAC", "B2B", "Order Fallout", "All"]

TOOLS = [
    {
        "name": "list_open_tickets",
        "description": (
            "Lists currently open (unresolved) tickets, optionally filtered "
            "by team and/or SLA breach status. Use this for questions like "
            "'what's breaching in B2B' or 'how many tickets are open in SAC'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": CATEGORY_ENUM,
                    "description": "Which team's queue to look at. Use 'All' for every team.",
                },
                "breach_status": {
                    "type": "string",
                    "enum": ["OK", "About to Breach", "Breached", "Unknown (no due date set)", "Any"],
                    "description": "Filter by SLA breach status. Use 'Any' for no filter.",
                },
            },
        },
    },
    {
        "name": "list_resolved_tickets",
        "description": (
            "Lists tickets resolved today, optionally filtered by team "
            "and/or whether they were resolved within SLA. Use this for "
            "questions like 'what did Retail resolve today' or 'which "
            "tickets breached SLA before being resolved'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": CATEGORY_ENUM,
                    "description": "Which team's queue to look at. Use 'All' for every team.",
                },
                "resolved_status": {
                    "type": "string",
                    "enum": ["Within SLA", "Breached", "Any"],
                    "description": "Filter by whether resolution beat the SLA deadline. Use 'Any' for no filter.",
                },
            },
        },
    },
    {
        "name": "get_productivity_summary",
        "description": (
            "Per-assignee workload: how many tickets each person currently "
            "has open and how many they resolved today, broken down by "
            "team. Use this for questions like 'who has the most open "
            "tickets' or 'how many did Jane resolve today'."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "categories": {
                    "type": "array",
                    "items": {"type": "string", "enum": CATEGORY_ENUM[:-1]},
                    "description": "Teams to include. Omit or leave empty for all teams.",
                },
            },
        },
    },
]


def _resolve_categories(selected, all_categories):
    if not selected or selected == "All":
        return list(all_categories)
    if isinstance(selected, str):
        return [selected] if selected in all_categories else []
    return [c for c in selected if c in all_categories]


def _safe_float(value):
    try:
        return None if value is None or value != value else round(float(value), 1)
    except (TypeError, ValueError):
        return None


def list_open_tickets(category, breach_status, category_data):
    categories = _resolve_categories(category, category_data.keys())
    tickets = []
    for cat in categories:
        df = category_data.get(cat)
        if df is None or df.empty:
            continue
        sub = df if not breach_status or breach_status == "Any" else df[df["Breach Status"] == breach_status]
        for _, row in sub.iterrows():
            tickets.append({
                "category": cat,
                "key": str(row["Key"]),
                "summary": str(row["Summary"]),
                "assignee": str(row["Assignee"]),
                "status": str(row["Status"]),
                "severity": str(row["Severity"]),
                "breach_status": str(row["Breach Status"]),
                "hours_remaining": _safe_float(row["Hours Remaining"]),
                "due": str(row["Due"]),
            })
    return {"count": len(tickets), "tickets": tickets}


def list_resolved_tickets(category, resolved_status, resolved_data):
    status_map = {"Within SLA": breach_logic.STATUS_OK, "Breached": breach_logic.STATUS_BREACHED}
    filter_status = status_map.get(resolved_status)

    categories = _resolve_categories(category, resolved_data.keys())
    tickets = []
    for cat in categories:
        df = resolved_data.get(cat)
        if df is None or df.empty:
            continue
        sub = df if filter_status is None else df[df["Resolved Status"] == filter_status]
        for _, row in sub.iterrows():
            tickets.append({
                "category": cat,
                "key": str(row["Key"]),
                "summary": str(row["Summary"]),
                "assignee": str(row["Assignee"]),
                "severity": str(row["Severity"]),
                "resolved_status": str(row["Resolved Status"]),
                "due": breach_logic.format_due_datetime(row["Due Datetime"]),
            })
    return {"count": len(tickets), "tickets": tickets}


def get_productivity_summary(categories, category_data, resolved_data):
    cats = _resolve_categories(categories, category_data.keys())
    per_open = defaultdict(lambda: defaultdict(int))
    per_resolved = defaultdict(lambda: defaultdict(int))

    for cat in cats:
        df = category_data.get(cat)
        if df is not None and not df.empty:
            for assignee, count in df["Assignee"].value_counts().items():
                per_open[assignee][cat] = int(count)
        rdf = resolved_data.get(cat)
        if rdf is not None and not rdf.empty:
            for assignee, count in rdf["Assignee"].value_counts().items():
                per_resolved[assignee][cat] = int(count)

    assignees = set(per_open) | set(per_resolved)
    rows = []
    for assignee in assignees:
        total_open = sum(per_open[assignee].get(cat, 0) for cat in cats)
        total_resolved = sum(per_resolved[assignee].get(cat, 0) for cat in cats)
        rows.append({
            "assignee": assignee,
            "open_by_team": {cat: per_open[assignee].get(cat, 0) for cat in cats},
            "resolved_today_by_team": {cat: per_resolved[assignee].get(cat, 0) for cat in cats},
            "total_open": total_open,
            "total_resolved_today": total_resolved,
        })
    rows.sort(key=lambda r: r["total_open"], reverse=True)
    return {"teams_included": cats, "assignees": rows}


def _execute_tool(name, tool_input, category_data, resolved_data):
    if name == "list_open_tickets":
        return list_open_tickets(tool_input.get("category"), tool_input.get("breach_status"), category_data)
    if name == "list_resolved_tickets":
        return list_resolved_tickets(tool_input.get("category"), tool_input.get("resolved_status"), resolved_data)
    if name == "get_productivity_summary":
        return get_productivity_summary(tool_input.get("categories"), category_data, resolved_data)
    return {"error": f"Unknown tool: {name}"}


def _client():
    if config.ANTHROPIC_API_KEY:
        return anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return anthropic.Anthropic()


def ask(question, history, category_data, resolved_data):
    """
    Runs one user turn through the tool-use loop.
    `history` is the raw prior messages list (including past tool_use/
    tool_result blocks); returns (answer_text, updated_history).
    """
    client = _client()
    messages = history + [{"role": "user", "content": question}]

    while True:
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=4096,
            system=SYSTEM_PROMPT,
            tools=TOOLS,
            output_config={"effort": "low"},
            messages=messages,
        )
        messages.append({"role": "assistant", "content": response.content})

        if response.stop_reason != "tool_use":
            text = "".join(block.text for block in response.content if block.type == "text")
            return text, messages

        tool_results = []
        for block in response.content:
            if block.type == "tool_use":
                result = _execute_tool(block.name, block.input, category_data, resolved_data)
                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": json.dumps(result),
                })
        messages.append({"role": "user", "content": tool_results})
