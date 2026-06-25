"""Audit log dashboard page (admin-only). Split from dashboard.py — see
private/dashboard-split-plan.md."""

import json
from collections import Counter

from fastapi import APIRouter, Request, Depends

from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    local_dt,
    _build_pagination,
)

router = APIRouter()


@router.get("/audit")
async def audit_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import audit_service

    if session.get("role") != "admin":
        return render_page(
            "Admin Required",
            """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">The audit log is available to administrators only.</p>
      <a href="/" class="btn btn-secondary">Back to Overview</a>
    </div>
    """,
            "/",
            session=session,
            status_code=403,
        )

    page = max(1, int(request.query_params.get("page", 1)))
    limit = 50
    offset = (page - 1) * limit

    actor_filter = request.query_params.get("actor_type", "")
    action_filter = request.query_params.get("action", "")
    resource_filter = request.query_params.get("resource_type", "")
    result_filter = request.query_params.get("result", "")

    from app.services.audit_service import ACTOR_TYPES, RESULT_TYPES, AUDIT_ACTIONS

    total_events = audit_service.count_events(
        actor_type=actor_filter or None,
        action=action_filter or None,
        resource_type=resource_filter or None,
        result=result_filter or None,
    )
    total_pages = max(1, (total_events + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit

    all_events = (
        audit_service.query_events(
            actor_type=actor_filter or None,
            action=action_filter or None,
            resource_type=resource_filter or None,
            result=result_filter or None,
            limit=limit,
            offset=offset,
        )
        or []
    )
    page_event_count = len(all_events)
    success_count = sum(1 for e in all_events if e.get("result") == "success")
    failure_count = sum(1 for e in all_events if e.get("result") == "failure")
    blocked_count = sum(1 for e in all_events if e.get("result") == "blocked")
    events_with_details = sum(1 for e in all_events if e.get("details_json"))
    page_start = offset + 1 if total_events else 0
    page_end = offset + page_event_count
    actor_counts = Counter(e.get("actor_type") or "unknown" for e in all_events)
    action_counts = Counter(e.get("action") or "unknown" for e in all_events)
    resource_counts = Counter(
        e.get("resource_type") or "-" for e in all_events if e.get("resource_type")
    )

    def details_preview(event: dict) -> str:
        raw = event.get("details_json")
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except Exception:
            return ""
        if not isinstance(parsed, dict) or not parsed:
            return ""
        preview_items = []
        for key, value in list(parsed.items())[:2]:
            val_str = json.dumps(value) if isinstance(value, (dict, list)) else str(value)
            val_truncated = val_str[:40] + ("..." if len(val_str) > 40 else "")
            preview_items.append(
                f"<span class='detail-item'><span class='detail-key'>{escape_html(key)}</span>: <span class='detail-val'>{escape_html(val_truncated)}</span></span>"
            )
        return "".join(preview_items)

    no_details_html = "<span class='text-muted'>No details</span>"

    top_actor_summary = "".join(
        f"<span class='badge badge-active' style='margin-right:8px;margin-bottom:8px'>{escape_html(actor)}: {count}</span>"
        for actor, count in actor_counts.most_common(4)
    )
    top_action_summary = "".join(
        f"<span class='badge badge-secondary' style='margin-right:8px;margin-bottom:8px'>{escape_html(action)}: {count}</span>"
        for action, count in action_counts.most_common(4)
    )
    top_resource_summary = "".join(
        f"<span class='badge badge-stale' style='margin-right:8px;margin-bottom:8px'>{escape_html(resource)}: {count}</span>"
        for resource, count in resource_counts.most_common(4)
    )

    def audit_row(event: dict) -> str:
        result_class = "active" if event.get("result", "") == "success" else "cancelled"
        details_html = details_preview(event) or no_details_html
        ts = event.get('timestamp')
        time_cell = (
            f"{local_dt(ts, style='date')}<br>"
            f"{local_dt(ts, style='time')}"
            if ts else "—"
        )
        return (
            f"<tr><td class='audit-time-cell'>{time_cell}</td>"
            f"<td class='audit-actor-cell'><span class='badge badge-secondary'>{event.get('actor_type', '')}</span></td>"
            f"<td class='audit-action-cell'><code>{event.get('action', '')}</code></td>"
            f"<td class='audit-resource-cell'>{event.get('resource_type', '') or '-'}</td>"
            f"<td class='audit-result-cell'><span class='badge badge-{result_class}'>{event.get('result', '')}</span></td>"
            f"<td class='mono audit-details-cell'>{details_html}</td>"
            f"<td class='mono audit-ip-cell'>{event.get('ip_address', '') or '-'}</td></tr>"
        )

    rows = "".join(audit_row(e) for e in all_events)

    def build_options(items, selected):
        return "".join(
            f"<option value='{i}' {'selected' if i == selected else ''}>{i}</option>"
            for i in items
        )

    actor_options = build_options(ACTOR_TYPES, actor_filter)
    action_options = build_options(AUDIT_ACTIONS, action_filter)
    result_options = build_options(RESULT_TYPES, result_filter)

    audit_extra_qs = "&amp;".join(
        f"{k}={v}"
        for k, v in [
            ("actor_type", actor_filter),
            ("action", action_filter),
            ("resource_type", resource_filter),
            ("result", result_filter),
        ]
        if v
    )
    pagination_html = _build_pagination(page, total_pages, audit_extra_qs)
    page_info = f"Page {page} of {total_pages} &nbsp;·&nbsp; Showing {page_start}–{page_end} of {total_events}"

    js = """
    <script>
    async function exportAuditCsv() {
      const params = new URLSearchParams(window.location.search);
      params.set('format', 'csv');
      const r = await fetch('/api/dashboard/audit/export?' + params.toString());
      if (r.ok) {
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'audit-log.csv'; a.click();
        URL.revokeObjectURL(url);
        showToast('CSV downloaded');
      } else { showToast('Export failed', 'danger'); }
    }
    function applyAuditFilters() {
      const actor = document.getElementById('audit-actor-type').value;
      const action = document.getElementById('audit-action').value;
      const resource = document.getElementById('audit-resource').value;
      const result = document.getElementById('audit-result').value;
      const params = new URLSearchParams();
      if (actor) params.set('actor_type', actor);
      if (action) params.set('action', action);
      if (resource) params.set('resource_type', resource);
      if (result) params.set('result', result);
      window.location.search = params.toString();
    }
    function clearAuditFilters() {
      window.location.search = '';
    }
    function openPruneModal(resourceType, title) {
      document.getElementById('prune-resource-type').value = resourceType;
      document.getElementById('prune-modal-title').textContent = title;
      document.getElementById('prune-before-date').value = '';
      document.getElementById('prune-result').innerHTML = '';
      openModal('prune-modal');
    }
    async function submitPrune(e) {
      e.preventDefault();
      const resourceType = document.getElementById('prune-resource-type').value;
      const beforeDate = document.getElementById('prune-before-date').value;
      if (!beforeDate) { showToast('Pick a cutoff date', 'warning'); return; }
      if (!confirm('Permanently prune ' + resourceType + ' data older than ' + beforeDate + '?')) return;
      const j = await apiFetch('/api/dashboard/prune', {
        method: 'POST',
        body: JSON.stringify({ resource_type: resourceType, before_date: beforeDate })
      });
      if (j.ok) {
        document.getElementById('prune-result').innerHTML =
          '<div class="alert alert-success">Deleted <code>' + j.data.deleted_count + '</code> ' + resourceType + ' records older than <code>' + beforeDate + '</code>.</div>';
        showToast('Prune complete');
        location.reload();
      } else {
        showToast(j.error?.message || 'Prune failed', 'danger');
      }
    }
    </script>"""

    return render_page(
        "Audit Log",
        f"""
    <div class="page-header"><h1>Audit Log</h1><div class="page-actions">
        <button class="btn btn-secondary" onclick="exportAuditCsv()">Export CSV</button>
        <button class="btn btn-danger" onclick="openPruneModal('audit','Prune Audit Log')">Prune Log</button>
    </div></div>
    <div class="card">
      <h3>Events</h3>
      <div class="card" style="margin-bottom:12px;padding:14px">
        <div class="section-header" style="margin-bottom:8px">
          <h3>Audit Snapshot</h3>
          <div class="section-note">A quick read on how much is happening and what kinds of events are being written.</div>
        </div>
        <div class="stat-grid" style="margin-bottom:10px">
          <div class="stat-card"><div class="value">{total_events}</div><div class="label">Total Events</div></div>
          <div class="stat-card"><div class="value">{success_count}</div><div class="label">Success</div></div>
          <div class="stat-card"><div class="value">{failure_count}</div><div class="label">Failure</div></div>
          <div class="stat-card"><div class="value">{blocked_count}</div><div class="label">Blocked</div></div>
          <div class="stat-card"><div class="value">{events_with_details}</div><div class="label">With Details</div></div>
        </div>
        <div class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Top actors:</div>
        <div>{top_actor_summary or "<span class='text-muted'>No events yet.</span>"}</div>
        <div class="text-muted" style="font-size:0.85rem;margin:12px 0 8px">Top actions:</div>
        <div>{top_action_summary or "<span class='text-muted'>No events yet.</span>"}</div>
        <div class="text-muted" style="font-size:0.85rem;margin:12px 0 8px">Top resources:</div>
        <div>{top_resource_summary or "<span class='text-muted'>No events yet.</span>"}</div>
      </div>
      <div class="filter-bar" style="margin-bottom:12px">
        <select id="audit-actor-type" style="width:120px">
          <option value="">Any actor</option>
          {actor_options}
        </select>
        <select id="audit-action" style="width:160px">
          <option value="">Any action</option>
          {action_options}
        </select>
        <select id="audit-resource" style="width:120px">
          <option value="">Any resource</option>
          <option value="agent" {"selected" if resource_filter == "agent" else ""}>agent</option>
          <option value="workspace" {"selected" if resource_filter == "workspace" else ""}>workspace</option>
          <option value="memory" {"selected" if resource_filter == "memory" else ""}>memory</option>
          <option value="credential" {"selected" if resource_filter == "credential" else ""}>credential</option>
          <option value="activity" {"selected" if resource_filter == "activity" else ""}>activity</option>
        </select>
        <select id="audit-result" style="width:120px">
          <option value="">Any result</option>
          {result_options}
        </select>
        <button class="btn btn-sm" onclick="applyAuditFilters()">Filter</button>
        <button class="btn btn-sm btn-secondary" onclick="clearAuditFilters()">Clear</button>
      </div>
      <div style="overflow-x:auto">
      <table class="audit-table"><thead><tr><th class="audit-time-cell">Time</th><th class="audit-actor-cell">Actor Type</th><th class="audit-action-cell">Action</th><th class="audit-resource-cell">Resource</th><th class="audit-result-cell">Result</th><th class="audit-details-cell">Details</th><th class="audit-ip-cell">IP</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=7 class=empty>No events yet.</td></tr>"}</tbody></table>
      </div>
      <div style="margin-top:8px;font-size:0.85rem;color:var(--text-muted)">{page_info}</div>
      {pagination_html}
    </div>

    <div class="modal-overlay" id="prune-modal" style="display:none">
      <div class="modal">
        <h3 id="prune-modal-title">Prune Data</h3>
        <form id="prune-form" onsubmit="submitPrune(event)">
          <input type="hidden" id="prune-resource-type" value="">
          <div class="form-group">
            <label>Delete records older than *</label>
            <input type="date" id="prune-before-date" required>
            <p class="form-hint">This permanently deletes audit rows older than the selected date. Use export first if you need a copy.</p>
          </div>
          <div id="prune-result" style="margin-top:8px"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('prune-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Prune</button>
          </div>
        </form>
      </div>
    </div>
    """,
        "/audit",
        js,
        session=session,
    )

