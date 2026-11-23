"""
The SLA miss comment list shared by the dashboard and the Historical Tickets page: one
box per breached ticket, showing the saved comment, with a pencil icon to edit it.
"""

import html
import time
from functools import partial

import streamlit as st

import sla_comments
from popup_ui import jira_ticket_url, severity_tag_html, status_tag_html


def render_comments(tickets, prefix):
    """tickets: dicts with key, summary, assignee, severity, status, escalation.
    prefix keeps the widget keys of the two pages apart."""
    if not tickets:
        st.caption("No breached tickets, so no SLA miss comments are needed.")
        return
    saved = sla_comments.load_all()
    for t in tickets:
        key = t["key"]
        note = saved.get(key)
        edit_flag = f"{prefix}-editing-{key}"
        with st.container(border=True):
            head, pen = st.columns([12, 1], vertical_alignment="center")
            with head:
                st.markdown(
                    f'<div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap;">'
                    f'<a href="{html.escape(jira_ticket_url(key), quote=True)}" target="_blank" rel="noopener" '
                    f'style="font-weight:700;">{html.escape(key)}</a>'
                    f'{severity_tag_html(t.get("severity"))}'
                    f'{status_tag_html(t.get("status"), t.get("escalation"))}'
                    f'<span style="color:#C3C2B7;">{html.escape(t.get("assignee") or "Unassigned")}</span>'
                    f'</div>'
                    f'<div style="color:#898781;font-size:0.8rem;">{html.escape(t.get("summary") or "")}</div>',
                    unsafe_allow_html=True)
            with pen:
                if st.button("✏️", key=f"{prefix}-pen-{key}", help="Edit SLA miss comment"):
                    st.session_state[edit_flag] = True
                    st.rerun(scope="fragment")

            if st.session_state.get(edit_flag):
                text = st.text_area("SLA miss comment", value=note["comment"] if note else "",
                                    key=f"{prefix}-text-{key}", height=110)
                save_col, cancel_col, _ = st.columns([1, 1, 4])
                if save_col.button("Save", key=f"{prefix}-save-{key}", type="primary"):
                    sla_comments.save(key, text)
                    st.session_state[edit_flag] = False
                    st.rerun(scope="fragment")
                if cancel_col.button("Cancel", key=f"{prefix}-cancel-{key}"):
                    st.session_state[edit_flag] = False
                    st.rerun(scope="fragment")
            elif note and note["comment"]:
                st.markdown(
                    f'<div style="white-space:pre-wrap;color:#F5F5F7;">{html.escape(note["comment"])}</div>'
                    f'<div style="color:#898781;font-size:0.72rem;margin-top:4px;">Last updated {html.escape(note["updated_at"])}</div>',
                    unsafe_allow_html=True)
            else:
                st.caption("No SLA miss comment yet. Click ✏️ to add one.")


OPEN_FOR_SECONDS = 30 * 60  # longest the dialog is treated as open (a missed close can't pause refresh forever)


def _state_key(prefix):
    return f"{prefix}-comments-state"


def comments_open(prefix):
    """True while this page's comments dialog is open. The page skips its auto-refresh then,
    so the dialog is not closed by a refresh while someone is typing."""
    state = st.session_state.get(_state_key(prefix))
    return bool(state) and (time.time() - state["at"]) < OPEN_FOR_SECONDS


def _close_comments(prefix):
    st.session_state.pop(_state_key(prefix), None)


def _dialog_body(tickets, prefix):
    teams = sorted({t["team"] for t in tickets if t.get("team")})
    sevs = sorted({t["severity"] for t in tickets if t.get("severity")})
    team_col, sev_col = st.columns(2)
    team = team_col.selectbox("Team", ["All teams", *teams], key=f"{prefix}-filter-team")
    sev = sev_col.selectbox("Severity", ["All", *sevs], key=f"{prefix}-filter-sev")
    shown = [t for t in tickets
             if (team == "All teams" or t.get("team") == team) and (sev == "All" or t.get("severity") == sev)]
    st.caption(f"{len(shown)} breached ticket(s) shown. Click ✏️ on a ticket to add or edit its comment.")
    render_comments(shown, prefix)


def open_comments_button(tickets, prefix, label):
    """Button that opens the SLA miss comments dialog. Each open gets its own dialog
    identity (the title carries an invisible counter), so a reopen always shows even if
    the browser missed the previous close. While open, the dialog is drawn again on each
    rerun, so it stays up."""
    if st.button(label, key=f"{prefix}-open-comments"):
        previous = st.session_state.get(_state_key(prefix)) or {}
        st.session_state[_state_key(prefix)] = {"at": time.time(), "n": previous.get("n", 0) + 1}
    state = st.session_state.get(_state_key(prefix))
    if state and comments_open(prefix):
        dialog = st.dialog("SLA miss comments" + "​" * state["n"], width="large",
                           on_dismiss=partial(_close_comments, prefix))(_dialog_body)
        dialog(tickets, prefix)
