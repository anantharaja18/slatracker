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

Cancelled tickets (status Canceled or Cancelled) are excluded everywhere:
open, inflow, backlog and resolved counts on the dashboard; both history
pages; and the SAL / SFS fixes matched to drivers.

## 6. Ticket Trend Analysis

The dashboard's **Ticket Trend Analysis** button opens a second page in a new
browser tab. It shows:

- **Top 5 drivers** for SAC, B2B, Order Fallout and SAT (Sales Assurance
  Team) over a day (default: today), a Sunday-to-Saturday week, or a custom
  date range. Each category is exactly the JQL in `TREND_CATEGORY_JQL` in
  `config.py`; a ticket matching several (e.g. a B2B order fallout) counts
  in each.
- **How drivers are grouped.** SAC and B2B use the Incident Sub Category
  field. Tickets without one get a theme from their summary keywords.
  SAT uses Issue Sub-Category, and its summaries add the issue identified by
  the SAT agent, the channel, order type and customer type.
  Order fallout tickets (raised by the SAC BOT) use Failed Step plus Error
  Description in whichever category they count, with order numbers
  and IDs stripped so that repeats of the same error group together.
- **Weekly trend** for each category's top drivers, over 4 to 26 weeks.
- **SAL / SFS tracking per driver.** Each driver lists the SAL tickets
  (development fixes) and SFS tickets that its tickets link to, plus any
  whose summary names the same failed step or error. Each fix shows its
  status and deployment date, or its planned date (fix version, *Final or
  Production ETA*, or due date).
- **Alerts:**
  - *Still occurring after fix*: a fix was deployed (or the SFS resolved),
    but tickets for the same issue kept arriving after it, at least one in
    the last 30 days.
  - *Fix date passed*: the planned date is over and the fix isn't deployed.
- **Download**: an Excel workbook with four sheets: Top Drivers, Fix
  Tracking (SAL-SFS), Weekly Trend and Tickets.

The data comes from a local cache, `trend_data/trend_cache.db`, holding about
1 year of tickets (`TREND_HISTORY_DAYS`). The first load takes about 10 minutes. After that, only
tickets updated since the last sync are fetched. To keep the cache live, run
the sync agent alongside the dashboard:

```bash
python trend_store.py --loop     # syncs every TREND_SYNC_MINUTES (10)
python trend_store.py --full     # reload the full year, e.g. after config changes
```

Without the agent running, the page syncs on its own whenever the cache is
more than 15 minutes old, and it refreshes every 5 minutes. Settings and
field IDs are under "Ticket Trend Analysis" in `config.py`.

## 7. Resolved Tickets History

The dashboard's **Resolved History** button opens a page in a new browser
tab. It covers Retail, SAC, B2B and Order Fallout over a day (default:
today), a Sunday-to-Saturday week, or a custom range, using the same
resolved-ticket queries (`resolved_scope_jql`) and SLA rule as the
dashboard's resolved cards. It shows:

- **Per-team tiles**: resolved count, SLA % (within SLA ÷ (within SLA +
  resolved late)), the change against the previous period, tickets created
  and resolved the same day, and the median time to resolve.
- **Trend** per day or per week: resolved tickets per team, each team on
  its own scale, plus SLA % for all teams.
- **Resolved by assignee** per team, with SLA totals.
- **The ticket list**, filterable by team, SLA status and text.
- **Excel download** with four sheets: Summary, Trend, By Assignee, Tickets.

It reads 1 year of resolved tickets from the same local cache and the same
sync as the trend page. Reopened tickets drop out at the next sync.

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
| `pages/Ticket_Trend_Analysis.py` | Ticket Trend Analysis page (opened from the dashboard) |
| `trend_store.py` | Jira -> local SQLite cache for the trend page; `--loop` = sync agent |
| `pages/Resolved_Tickets_History.py` | Resolved Tickets History page (opened from the dashboard) |
| `resolved_analysis.py` | Resolved history: per-team SLA, trends, by assignee, ticket list |
| `ui_common.py` | Shared look, period picker, sync trigger and Excel styling for both pages |
| `trend_analysis.py` | Driver grouping, top drivers, weekly trends, SAL / SFS fix matching and alerts |
