"""Hover popups for ticket lists, shared by the dashboard and the Historical Tickets page:
the popup markup, its CSS, and the JavaScript that opens and closes them."""

import html
import itertools

import config
from ui_common import SURFACE_CARD, TEXT_MUTED, TEXT_PRIMARY, TEXT_SECONDARY
from ui_table import DEFAULT_STATUS_DOT_COLOR, SEVERITY_TAG_COLORS, STATUS_DOT_COLOR


def status_dot_color(status):
    return STATUS_DOT_COLOR.get((status or "").strip().lower(), DEFAULT_STATUS_DOT_COLOR)

def jira_ticket_url(key):
    return f'{config.JIRA_BASE_URL.rstrip("/")}/browse/{key}'


def bucket_popup_html(tickets):
    if not tickets:
        return '<div class="bucket-popup-inner"><div class="bucket-popup-empty">No tickets</div></div>'
    # data-* attributes feed the popup's CSV download (see POPUP_SCRIPT).
    items = "".join(
        f'<a class="bucket-popup-item" href="{html.escape(jira_ticket_url(t["key"]), quote=True)}" '
        f'target="_blank" rel="noopener noreferrer" '
        f'data-key="{html.escape(t["key"], quote=True)}" '
        f'data-severity="{html.escape(t.get("severity") or "", quote=True)}" '
        f'data-assignee="{html.escape(t["assignee"], quote=True)}" '
        f'data-status="{html.escape(t.get("status") or "", quote=True)}" '
        f'data-created="{html.escape(t.get("created") or "", quote=True)}" '
        f'data-escalation="{html.escape(t.get("escalation") or "", quote=True)}" '
        f'data-due="{html.escape(t.get("due") or "", quote=True)}" '
        f'data-summary="{html.escape(t.get("summary") or "", quote=True)}">'
        f'<span class="bucket-popup-key">{html.escape(t["key"])}</span>'
        f'{severity_tag_html(t.get("severity"))}'
        f'{status_tag_html(t.get("status"), t.get("escalation"))}'
        f'<span class="bucket-popup-assignee" title="{html.escape(t["assignee"])}">{html.escape(t["assignee"])}</span></a>'
        for t in tickets
    )
    return f'<div class="bucket-popup-inner">{items}</div>'


def status_tag_html(status, escalation=None):
    """Current Jira status (Escalated, In Progress, ...) as a pill, with the
    same dot color as the Tickets by Status cards. An Escalated ticket also shows
    its escalation path (e.g. "Escalated · Network")."""
    if not status:
        return ""
    label = status
    if escalation and status.strip().lower() == "escalated":
        label = f"{status} · {escalation}"
    return (
        f'<span class="bucket-popup-status"><span class="dot" style="background:{status_dot_color(status)};"></span>'
        f'{html.escape(label)}</span>'
    )


def severity_tag_html(severity):
    if not severity:
        return ""
    bg, fg = SEVERITY_TAG_COLORS.get(severity, (DEFAULT_STATUS_DOT_COLOR, TEXT_PRIMARY))
    return f'<span class="bucket-popup-tag" style="background:{bg};color:{fg};">{html.escape(severity)}</span>'


_popup_counter = itertools.count()

# Export groups for the section-level "Download breached" buttons.
TTR_BREACHED_GROUP = "ttr-breached"            # Time to Resolve: Breached (Past) + Breached Today
RESOLVED_BREACHED_GROUP = "resolved-breached"  # Today's inflow: Resolved late + Open, breached

DOWNLOAD_ICON_SVG = (
    '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" '
    'stroke-width="2.2" stroke-linecap="round" stroke-linejoin="round">'
    '<path d="M12 4v11"/><path d="M7 10l5 5 5-5"/><path d="M5 20h14"/></svg>'
)


def popup_row(row_inner_html, popup_inner_html, title, row_class="bucket-row", export_group=None):
    """A bucket row (or other element, via row_class) whose ticket popup opens
    on hover and closes a few seconds after the pointer leaves it (or via its
    x). Handled by POPUP_SCRIPT. Popups sharing an export_group are downloaded
    together by a section_title_with_download() button."""
    popup_id = f"popup-{next(_popup_counter)}"
    group_attr = f' data-export-group="{html.escape(export_group, quote=True)}"' if export_group else ""
    return (
        f'<div class="popup-wrap" data-popup-id="{popup_id}"{group_attr}>'
        f'<div class="{row_class} has-popup">{row_inner_html}</div>'
        f'<div class="bucket-popup"><div class="bucket-popup-box">'
        f'<div class="bucket-popup-header"><span class="bucket-popup-title">{html.escape(title)}</span>'
        f'<span class="bucket-popup-actions">'
        f'<span class="bucket-popup-download" title="Download this list (CSV)">{DOWNLOAD_ICON_SVG}</span>'
        f'<span class="bucket-popup-close" title="Close">&times;</span></span></div>'
        f'{popup_inner_html}</div></div>'
        f'</div>'
    )


def section_title_with_download(title_html, export_group, button_label, file_name):
    """A section title bar with a button on the right that downloads every
    popup in export_group as one CSV."""
    return (
        f'<div class="section-title section-title-bar"><span>{title_html}</span>'
        f'<span class="section-download" data-export-group="{html.escape(export_group, quote=True)}" '
        f'data-file-name="{html.escape(file_name, quote=True)}" title="Download as CSV">'
        f'{DOWNLOAD_ICON_SVG}<span>{html.escape(button_label)}</span></span></div>'
    )


# Hovering a row briefly opens its popup (closing any other open one); it
# closes 5 seconds after the pointer leaves the row and popup, or at once when
# its x is clicked. The open popup's id is remembered so it's
# re-opened after a Streamlit rerun (e.g. the 1-minute auto-refresh) redraws it.
POPUP_SCRIPT = """
<script>
(function () {
  const win = window.parent && window.parent.document ? window.parent : window;
  const doc = win.document;
  if (win.__slaPopupsInstalled) return;
  win.__slaPopupsInstalled = true;
  win.__slaOpenPopup = null;

  function setOpen(id) {
    win.__slaOpenPopup = id;
    doc.querySelectorAll(".popup-wrap.open").forEach(function (w) {
      if (w.dataset.popupId !== id) w.classList.remove("open");
    });
    if (id) {
      const w = doc.querySelector('.popup-wrap[data-popup-id="' + id + '"]');
      if (w) openWrap(w);
    }
  }

  // Shows a popup, flipping it to align with the row's right edge if it
  // would otherwise run past the right side of the window.
  function openWrap(w) {
    w.classList.add("open");
    placePopup(w);
  }

  // The popup is fixed to the window (so no table or page container clips it): below the row when
  // there is room, otherwise above it, kept inside the window on the left and right.
  function placePopup(w) {
    const pop = w.querySelector(".bucket-popup");
    if (!pop) return;
    const r = w.getBoundingClientRect();
    pop.style.position = "fixed";
    pop.style.right = "auto";
    pop.style.bottom = "auto";
    pop.style.minWidth = r.width + "px";
    pop.style.left = "0px";
    pop.style.top = "0px";
    const pw = pop.offsetWidth, ph = pop.offsetHeight;
    let left = r.left;
    if (left + pw > win.innerWidth - 8) left = Math.max(8, r.right - pw);
    let top = r.bottom;
    if (top + ph > win.innerHeight - 8) top = Math.max(8, r.top - ph);
    pop.style.left = left + "px";
    pop.style.top = top + "px";
  }

  // Keeps an open popup next to its row while the page scrolls.
  doc.addEventListener("scroll", function () {
    const open = doc.querySelector(".popup-wrap.open");
    if (open) placePopup(open);
  }, true);

  // Row left under the pointer when a popup is closed; it must not instantly
  // re-open, so it's ignored until the pointer moves off it.
  let suppressRow = null;

  // A row must be hovered briefly before its popup opens, so sweeping the
  // mouse across the page doesn't flash popups. Once the pointer leaves the
  // open row/popup, it closes after a grace period unless the pointer returns.
  const OPEN_DELAY_MS = 500;
  const CLOSE_DELAY_MS = 5000;
  let openTimer = null, pendingId = null, closeTimer = null;

  function cancelOpen() {
    clearTimeout(openTimer);
    openTimer = null;
    pendingId = null;
  }
  function cancelClose() {
    clearTimeout(closeTimer);
    closeTimer = null;
  }
  function scheduleClose() {
    if (!win.__slaOpenPopup || closeTimer) return;
    closeTimer = setTimeout(function () {
      closeTimer = null;
      setOpen(null);
    }, CLOSE_DELAY_MS);
  }

  doc.addEventListener("mouseover", function (e) {
    const wrap = e.target.closest && e.target.closest(".popup-wrap");
    const row = e.target.closest && e.target.closest(".has-popup");
    if (suppressRow && row !== suppressRow) suppressRow = null;

    if (wrap && wrap.dataset.popupId === win.__slaOpenPopup) {
      cancelClose();
      cancelOpen();
      return;
    }
    scheduleClose();
    if (!row || row === suppressRow) {
      cancelOpen();
      return;
    }
    const id = row.closest(".popup-wrap").dataset.popupId;
    if (id === pendingId) return;
    cancelOpen();
    pendingId = id;
    openTimer = setTimeout(function () {
      openTimer = null;
      pendingId = null;
      cancelClose();
      setOpen(id);
    }, OPEN_DELAY_MS);
  });

  // Pointer left the window entirely.
  doc.addEventListener("mouseout", function (e) {
    if (!e.relatedTarget) {
      cancelOpen();
      scheduleClose();
    }
  });

  // Saves a popup's ticket list as CSV, built from the popup's own rows so it
  // always matches the count that was hovered.
  function csvCell(v) {
    return '"' + String(v == null ? "" : v).replace(/"/g, '""') + '"';
  }
  // One or more popups into one CSV. Popup titles are "Team, em dash, List".
  // (No angle brackets in this script: Streamlit's HTML handling drops it.)
  const TITLE_SEP = " " + String.fromCharCode(8212) + " ";
  function downloadPopups(wraps, fileBase) {
    const lines = [["S.No", "Team", "List", "Key", "Severity", "Status", "Assignee", "Created", "Due", "Escalation Path", "Summary", "Jira Link"]];
    wraps.forEach(function (wrap) {
      const titleEl = wrap.querySelector(".bucket-popup-title");
      const parts = (titleEl ? titleEl.textContent.trim() : "").split(TITLE_SEP);
      const team = parts.length > 1 ? parts[0] : "";
      const list = parts.length > 1 ? parts.slice(1).join(TITLE_SEP) : parts[0];
      wrap.querySelectorAll(".bucket-popup-item").forEach(function (a) {
        const d = a.dataset;
        const key = d.key || (a.querySelector(".bucket-popup-key") || a).textContent.trim();
        lines.push([lines.length, team, list, key, d.severity, d.status, d.assignee, d.created, d.due, d.escalation, d.summary, a.href]);
      });
    });
    if (lines.length < 2) return;
    const csv = "\\ufeff" + lines.map(function (r) { return r.map(csvCell).join(","); }).join("\\r\\n");
    const blob = new Blob([csv], { type: "text/csv;charset=utf-8" });
    const url = URL.createObjectURL(blob);
    const link = doc.createElement("a");
    const now = new Date();  // local date, not UTC, so it matches the dashboard's "today"
    const stamp = now.getFullYear() + "-" + String(now.getMonth() + 1).padStart(2, "0") + "-" + String(now.getDate()).padStart(2, "0");
    link.href = url;
    link.download = fileBase.replace(/[^A-Za-z0-9]+/g, "_").replace(/^_|_$/g, "") + "_" + stamp + ".csv";
    doc.body.appendChild(link);
    link.click();
    link.remove();
    setTimeout(function () { URL.revokeObjectURL(url); }, 1000);
  }

  doc.addEventListener("click", function (e) {
    const dl = e.target.closest && e.target.closest(".bucket-popup-download");
    if (dl) {
      e.preventDefault();
      const wrap = dl.closest(".popup-wrap");
      const titleEl = wrap.querySelector(".bucket-popup-title");
      downloadPopups([wrap], titleEl ? titleEl.textContent.trim() : "tickets");
      return;
    }
    // Section-level download: every popup tagged with the same export group.
    const sdl = e.target.closest && e.target.closest(".section-download");
    if (sdl) {
      e.preventDefault();
      const group = sdl.dataset.exportGroup;
      downloadPopups(doc.querySelectorAll('.popup-wrap[data-export-group="' + group + '"]'), sdl.dataset.fileName || group);
      return;
    }
    if (e.target.closest && e.target.closest(".bucket-popup-close")) {
      e.preventDefault();
      cancelOpen();
      cancelClose();
      setOpen(null);
      const under = doc.elementFromPoint(e.clientX, e.clientY);
      suppressRow = under && under.closest ? under.closest(".has-popup") : null;
    }
  });

  new win.MutationObserver(function () {
    const id = win.__slaOpenPopup;
    if (!id) return;
    const w = doc.querySelector('.popup-wrap[data-popup-id="' + id + '"]');
    if (w && !w.classList.contains("open")) openWrap(w);
  }).observe(doc.body, { childList: true, subtree: true });
})();
</script>
"""


POPUP_CSS = f"""
        .bucket-popup {{
            position: absolute; left: 0; top: 100%; z-index: 30;
            width: max-content; min-width: 100%; max-width: 500px;
            padding-top: 4px;
            display: none;  /* not just invisible: hidden popups must not widen the page */
        }}
        .bucket-popup-box {{
            background: {SURFACE_CARD}; border: 1px solid rgba(255,255,255,0.12); border-radius: 10px;
            box-shadow: 0 12px 30px rgba(0,0,0,0.5); padding: 6px;
        }}
        .bucket-popup-inner {{ max-height: 260px; overflow-y: auto; }}
        .bucket-popup-header {{
            display: flex; align-items: center; justify-content: space-between; gap: 12px;
            padding: 2px 4px 6px 10px; margin-bottom: 4px; border-bottom: 1px solid rgba(255,255,255,0.08);
            font-size: 0.72rem; font-weight: 700; letter-spacing: 0.04em; text-transform: uppercase; color: {TEXT_MUTED};
        }}
        .bucket-popup-close {{
            cursor: pointer; font-size: 1.1rem; line-height: 1; padding: 2px 8px; border-radius: 6px;
            color: {TEXT_SECONDARY}; text-transform: none;
        }}
        .bucket-popup-close:hover {{ background: rgba(255,255,255,0.1); color: {TEXT_PRIMARY}; }}
        .bucket-popup-actions {{ display: flex; align-items: center; gap: 2px; flex-shrink: 0; }}
        .bucket-popup-download {{
            cursor: pointer; display: flex; align-items: center; padding: 4px 7px; border-radius: 6px;
            color: {TEXT_SECONDARY};
        }}
        .bucket-popup-download:hover {{ background: rgba(255,255,255,0.1); color: {TEXT_PRIMARY}; }}
        .bucket-popup-box:not(:has(.bucket-popup-item)) .bucket-popup-download {{ display: none; }}
        .popup-wrap {{ position: relative; }}
        .bucket-row.has-popup {{ cursor: pointer; }}
        .bucket-row.has-popup:hover {{ background: rgba(255,255,255,0.04); }}
        .bucket-row.has-popup .label {{ cursor: pointer; }}
        .popup-wrap.open .bucket-popup {{ display: block; z-index: 40; }}
        .bucket-popup.align-right {{ left: auto; right: 0; }}
        .bucket-popup-item {{
            display: flex; align-items: center; justify-content: space-between; gap: 14px;
            padding: 6px 10px; border-radius: 6px; text-decoration: none !important;
            font-size: 0.82rem; color: {TEXT_SECONDARY};
        }}
        .bucket-popup-item:hover {{ background: rgba(255,255,255,0.06); color: {TEXT_PRIMARY}; }}
        .bucket-popup-sublink {{ padding-left: 20px; font-size: 0.74rem; opacity: 0.9; }}
        .bucket-popup-tag {{
            font-size: 0.6rem; font-weight: 700; padding: 2px 7px; border-radius: 999px;
            color: white; white-space: nowrap; flex-shrink: 0;
        }}
        .bucket-popup-key + .bucket-popup-tag {{ margin-left: -6px; margin-right: auto; }}  /* sits next to the key */
        .bucket-popup-status {{
            display: inline-flex; align-items: center; gap: 5px; flex-shrink: 0; white-space: nowrap;
            font-size: 0.66rem; font-weight: 600; color: {TEXT_SECONDARY};
            padding: 1px 8px; border-radius: 999px; border: 1px solid rgba(255,255,255,0.16);
        }}
        .bucket-popup-status .dot {{ width: 6px; height: 6px; border-radius: 50%; display: inline-block; }}
        .bucket-popup-key + .bucket-popup-status {{ margin-left: -6px; margin-right: auto; }}  /* no severity tag */
        .bucket-popup-key {{ font-weight: 700; color: {TEXT_PRIMARY}; white-space: nowrap; flex-shrink: 0; }}
        .bucket-popup-assignee {{
            text-align: right; white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
            min-width: 0; max-width: 220px;
        }}
        .bucket-popup-empty {{ padding: 8px; font-size: 0.8rem; color: {TEXT_MUTED}; text-align: center; }}
"""
