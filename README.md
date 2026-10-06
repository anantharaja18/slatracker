# Jira Breach Monitor

Monitors 4 Jira ticket categories (Retail, SAC, B2B, Order Fallout),
flags tickets that are about to breach or have breached, shows
them in a live dashboard, and sends Microsoft Teams alerts.

## 1. Install dependencies

```bash
pip install -r requirements.txt
```

## 2. Configure

Open `config.py` and fill in:

- `JIRA_BASE_URL`, `JIRA_EMAIL`, `JIRA_API_TOKEN` — your Jira Cloud API
  token (create one at id.atlassian.com -> Security -> API tokens).
- `TEAMS_CHANNEL_WEBHOOK_URL` — an Incoming Webhook URL from the Teams
  channel you want alerts posted to (channel -> "..." -> Connectors ->
  Incoming Webhook).
- Check each category's `breach_field` under `CATEGORIES` — these are
  marked `# TODO` where the actual SLA/due-date field needs confirming
  against your real Jira setup. `cf[10883]` was used for Retail based
  on your JQL's sort order; verify it's actually a due-date field, not
  something else (e.g. via Jira admin -> Custom fields, or by checking the
  field on a real ticket's "..." -> Configure details).

You can hardcode values directly in `config.py`, or set them as
environment variables of the same name (recommended so the API token isn't
committed to version control).

## 3. Run the dashboard

```bash
streamlit run dashboard.py
```

Opens a browser tab with tabs for each category, color-coded by breach
status, with a manual refresh button and status filters.

## 4. Run the scheduled Teams alerts

```bash
python checker.py
```

This runs forever, checking every `CHECK_INTERVAL_MINUTES` (default 30) and
posting a Teams alert for any ticket that newly becomes "About to Breach"
or "Breached". It won't re-alert the same ticket for the same status twice
(tracked in `notified_state.json`).

To run it as a background service instead of a terminal window:
- **Linux/Mac**: use a `systemd` service or `nohup python checker.py &`
- **Windows**: use Task Scheduler with `CHECK_INTERVAL_MINUTES = 0` and
  trigger it every 30 minutes, or run it via NSSM as a service

## 5. About Teams DMs

Right now, alerts go to the shared channel webhook with the assignee named
in the message text — this is the simplest setup and needs no extra Azure
setup. If you later want true 1:1 DMs to the assignee, you'll need a
Microsoft Graph API app registration (tenant ID, client ID, client secret)
with `Chat.Create` and `ChatMessage.Send` permissions — add those to
`config.py`, set `GRAPH_ENABLED = True`, and implement
`send_direct_message()` in `teams_notifier.py` using the Graph `/chats`
endpoint.

## File overview

| File | Purpose |
|---|---|
| `config.py` | Jira/Teams credentials, per-category JQL + breach rules |
| `jira_client.py` | Runs JQL searches against the Jira REST API |
| `breach_logic.py` | Computes OK / About to Breach / Breached per ticket |
| `teams_notifier.py` | Posts alerts to Teams |
| `state.py` | Dedup tracking so tickets aren't re-alerted every cycle |
| `checker.py` | Scheduled script — detection + Teams alerts |
| `dashboard.py` | Streamlit live dashboard UI |
