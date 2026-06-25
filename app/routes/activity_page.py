"""Activity dashboard page. Split from dashboard.py — see
private/dashboard-split-plan.md."""

from collections import Counter

from fastapi import APIRouter, Request, Depends

from app.branding import JS_WINDOW_EVENT
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    local_dt,
    _build_pagination,
)

router = APIRouter()


@router.get("/activity")
async def activity_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import activity_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"

    page = max(1, int(request.query_params.get("page", 1)))
    status_filter = request.query_params.get("status", "")
    limit = 50
    scope_user_id = None if is_admin else session["user_id"]

    total_activities = activity_service.count_activities(
        user_id=scope_user_id,
        status=status_filter or None,
    )
    total_pages = max(1, (total_activities + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit

    activities = (
        activity_service.list_activities(
            user_id=scope_user_id,
            status=status_filter or None,
            limit=limit,
            offset=offset,
        )
        or []
    )
    all_agents = (
        list_agents() if is_admin else list_agents(owner_user_id=session["user_id"])
    )
    agent_labels = {
        a["id"]: a.get("display_name") or a["id"] for a in all_agents
    }
    agent_options = "".join(
        f'<option value="{a["id"]}">{a.get("display_name", a["id"])}</option>'
        for a in all_agents
    )
    workspaces = (
        workspace_service.list_workspaces()
        if is_admin
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )
    user_scope = f"user:{session['user_id']}"
    activity_scope_options = (
        f'<option value="{user_scope}">Personal user memory ({user_scope})</option>'
        + "".join(
            f'<option value="workspace:{p["id"]}">Workspace: {escape_html(p.get("name") or p["id"])} (workspace:{p["id"]})</option>'
            for p in workspaces
        )
        + "".join(
            f'<option value="agent:{a["id"]}">Agent private: {escape_html(a.get("display_name") or a["id"])} (agent:{a["id"]})</option>'
            for a in all_agents
        )
    )
    # For reassign modal, we want the same options
    reassign_options = agent_options
    coordination_activities = [
        a for a in activities if a.get("status") in ("active", "stale", "blocked")
    ]
    attention_activities = [
        a for a in activities if a.get("status") in ("stale", "blocked")
    ]
    owner_counts = Counter(
        a.get("assigned_agent_id") or a.get("agent_id") or "unassigned"
        for a in coordination_activities
    )
    handoff_count = sum(1 for a in activities if a.get("reassigned_from_agent_id"))
    active_owner_count = len(owner_counts)
    coordination_summary = "".join(
        f"<span class='badge badge-active' style='margin-right:8px;margin-bottom:8px'>{escape_html(agent_labels.get(agent_id, agent_id))}: {count}</span>"
        for agent_id, count in owner_counts.most_common(6)
    )

    status_filters = [
        "active",
        "stale",
        "reassigned",
        "completed",
        "blocked",
        "cancelled",
    ]

    def _status_tab(s):
        active = s == (status_filter or "")
        cls = "btn btn-sm" + ("" if active else " btn-secondary")
        return f"<a href='?page=1&amp;status={s}' class='{cls}'>{s.title()}</a>"

    all_tab_cls = "btn btn-sm" + ("" if not status_filter else " btn-secondary")
    status_tabs = f"<a href='?page=1' class='{all_tab_cls}'>All</a>" + "".join(
        _status_tab(s) for s in status_filters
    )

    act_extra_qs = f"status={status_filter}" if status_filter else ""
    activity_pagination_html = _build_pagination(page, total_pages, act_extra_qs)
    page_start_act = offset + 1 if total_activities else 0
    page_end_act = offset + len(activities)
    activity_page_info = f"Page {page} of {total_pages} &nbsp;·&nbsp; Showing {page_start_act}–{page_end_act} of {total_activities}"
    activity_prune_button = (
        '<button class="btn btn-secondary" onclick="openPruneModal(\'activity\', \'Prune Activity History\')">Prune History</button>'
        if is_admin
        else ""
    )

    rows = "".join(
        f"<tr class='activity-row' data-status='{a.get('status', '')}'>"
        f"<td class='activity-task-cell'>{a.get('task_description', '')[:120]}</td>"
        f"<td class='activity-scope-cell'><code>{escape_html((a.get('memory_scope') or '').replace('workspace:', '')) or '—'}</code></td>"
        f"<td class='activity-status-cell'><span class='badge badge-{a.get('status', 'active')}'>{a.get('status', '')}</span></td>"
        f"<td class='activity-agent-cell'>{escape_html(agent_labels.get(a.get('assigned_agent_id', ''), a.get('assigned_agent_id', '')))}</td>"
        f"<td class='activity-handoff-cell'>{escape_html(agent_labels.get(a.get('reassigned_from_agent_id', ''), a.get('reassigned_from_agent_id', '')))}</td>"
        f"<td class='activity-updated-cell'>{local_dt(a.get('updated_at'), style='date')}<br>{local_dt(a.get('updated_at'), style='time')}</td>"
        f"<td><div class='actions-cell activity-actions-cell'>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"createHandoff('{a['id']}')\">Briefing</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"reassignActivity('{a['id']}')\" title='Reassign'>Reassign</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"updateActivity('{a['id']}','active')\" {'disabled' if a.get('status') not in ('stale', 'blocked', 'reassigned') else ''} title='Reactivate'>Start</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"updateActivity('{a['id']}','completed')\" {'disabled' if a.get('status') not in ('active', 'stale', 'blocked', 'reassigned') else ''} title='Complete'>Done</button>"
        f"<button type='button' class='btn btn-sm btn-danger' onclick=\"cancelActivity('{a['id']}')\" {'disabled' if a.get('status') not in ('active', 'stale', 'blocked', 'reassigned') else ''} title='Cancel'>Cancel</button>"
        f"</div></td></tr>"
        for a in activities
    )

    js = """
    <script>
    async function refreshActivity() {
      const params = new URLSearchParams(window.location.search);
      window.location.href = '/activity?' + params.toString();
    }
    window.onAgentCoreEvent = function(event) {
      if (!(event.type || '').startsWith('activity_')) return;
      var existing = document.getElementById('activity-live-banner');
      if (existing) return;
      var banner = document.createElement('div');
      banner.id = 'activity-live-banner';
      banner.className = 'alert';
      banner.style.cssText = 'margin:0 0 12px;display:flex;align-items:center;gap:10px';
      banner.innerHTML = '<span>Activity updated in the background.</span>' +
        '<button class="btn btn-sm btn-secondary" onclick="location.reload()">Refresh</button>';
      var table = document.querySelector('.activity-table');
      if (table && table.parentNode) table.parentNode.insertBefore(banner, table);
    };
    async function updateActivity(id, status) {
      const j = await apiFetch('/api/activity/' + id, { method: 'PUT', body: JSON.stringify({ status }) });
      if (j.ok) { showToast('Updated'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function cancelActivity(id) {
      if (!confirm('Cancel this activity?')) return;
      const j = await apiFetch('/api/activity/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('Cancelled'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
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
        refreshActivity();
      } else {
        showToast(j.error?.message || 'Prune failed', 'danger');
      }
    }
    async function createHandoff(id) {
      const j = await apiFetch('/api/briefings/handoff', { method: 'POST', body: JSON.stringify({ activity_id: id }) });
      if (j.ok) {
        const b = j.data.briefing;
        const section = function(title, items, emptyLabel) {
          const rows = (items || []).map(function(item) {
            const label = item.content || item.description || item.task_description || '';
            const meta = item.task_result || item.task_note || item.outcome || item.ended_at || item.started_at || item.generated_at || '';
            return '<li><div style="font-weight:600">' + escapeHtml(label).substring(0, 120) + '</div>' +
              (meta ? '<div class="text-muted" style="font-size:0.85rem">' + escapeHtml(String(meta)).substring(0, 60) + '</div>' : '') +
              '</li>';
          }).join('');
          return '<div style="margin-top:12px"><h4 style="margin-bottom:8px">' + escapeHtml(title) + '</h4>' +
            '<ul style="margin:0;padding-left:20px">' + (rows || '<li class="text-muted">' + escapeHtml(emptyLabel) + '</li>') + '</ul></div>';
        };
        const meta = [
          '<div class="card" style="margin-bottom:12px;padding:12px">',
          '<div class="text-muted" style="font-size:0.85rem">Source activity</div>',
          '<div style="font-weight:600">' + escapeHtml(b.task_description || '') + '</div>',
          (b.task_note ? '<div class="text-muted" style="font-size:0.85rem;margin-top:4px">Note: ' + escapeHtml(b.task_note) + '</div>' : ''),
          (b.task_result ? '<div class="text-muted" style="font-size:0.85rem;margin-top:4px">Result: ' + escapeHtml(b.task_result) + '</div>' : ''),
          '<div class="text-muted" style="font-size:0.85rem;margin-top:4px">',
          'Agent: ' + escapeHtml(b.agent_id || '') + ' | Assigned: ' + escapeHtml(b.assigned_agent_id || '') + ' | Generated: ' + escapeHtml((b.generated_at || '').substring(0, 19)),
          '</div>',
          '</div>'
        ].join('');
        document.getElementById('briefing-content').innerHTML =
          meta +
          section('Facts', b.facts, 'No facts') +
          section('Decisions', b.decisions, 'No decisions') +
          section('Preferences', b.preferences, 'No preferences') +
          section('Recent Completed', b.recent_completed, 'No recent completed activity');
        openModal('briefing-modal');
      } else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function doCreateActivity(e) {
      e.preventDefault();
      const agentId = document.getElementById('act-agent').value;
      const body = {
        assigned_agent_id: agentId,
        task_description: document.getElementById('act-task').value,
        memory_scope: document.getElementById('act-memory-scope').value || ('agent:' + agentId),
      };
      const j = await apiFetch('/api/activity', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) { showToast('Activity created'); closeModal('create-activity-modal'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function reassignActivity(id) {
      document.getElementById('reassign-activity-id').value = id;
      openModal('reassign-modal');
    }
    async function doReassign() {
      const id = document.getElementById('reassign-activity-id').value;
      const agent = document.getElementById('reassign-agent-select').value;
      if (!agent) { showToast('Select an agent', 'warning'); return; }
      const j = await apiFetch('/api/activity/' + id + '/recovery', {
        method: 'POST', body: JSON.stringify({ action: 'reassign_to_agent', new_agent_id: agent })
      });
      if (j.ok) { showToast('Reassigned'); closeModal('reassign-modal'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    function copyGeneratedOutput(btn) {
      copyToClipboard(document.querySelector('#ig-output pre').textContent, btn);
    }

    function escapeHtml(s) {
      return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    </script>"""
    js = js.replace("window.onAgentCoreEvent", "window." + JS_WINDOW_EVENT)

    return render_page(
        "Activity",
        f"""
    <div class="page-header"><h1>Activity</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-activity-modal')">+ Assign Work</button>
        {activity_prune_button}
    </div></div>
    <div class="card">
      <h3>Tasks</h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Assign work to agents and track progress. An agent session calls <code>activity_pickup</code> at startup or when idle to claim tasks assigned to it in its configured workspace. Use reassign and briefing when work needs to move between agents.</p>
      <div class="card" style="margin-bottom:12px;padding:14px">
        <div class="section-header" style="margin-bottom:8px">
          <h3>Coordination Snapshot</h3>
          <div class="section-note">Who owns work right now and where handoffs have happened.</div>
        </div>
        <div class="stat-grid" style="margin-bottom:10px">
          <div class="stat-card"><div class="value">{len(coordination_activities)}</div><div class="label">Open Work Items</div></div>
          <div class="stat-card"><div class="value">{active_owner_count}</div><div class="label">Assigned Agents</div></div>
          <div class="stat-card"><div class="value">{handoff_count}</div><div class="label">Recent Handoffs</div></div>
          <div class="stat-card"><div class="value">{len(attention_activities)}</div><div class="label">Needs Attention</div></div>
        </div>
        <div class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Current ownership:</div>
        <div>{coordination_summary or "<span class='text-muted'>No open work items yet.</span>"}</div>
      </div>
      <div class="filter-bar" style="margin-bottom:12px">
        {status_tabs}
      </div>
      <div style="overflow-x:auto">
      <table class="activity-table"><thead><tr><th class="activity-task-cell">Task</th><th class="activity-scope-cell">Scope</th><th class="activity-status-cell">Status</th><th class="activity-agent-cell">Agent</th><th class="activity-handoff-cell">Handoff From</th><th class="activity-updated-cell">Updated</th><th class="activity-actions-header">Actions</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=7 class=empty>No activities yet.</td></tr>"}</tbody></table>
      </div>
      <div style="margin-top:8px;font-size:0.85rem;color:var(--text-muted)">{activity_page_info}</div>
      {activity_pagination_html}
    </div>

    <!-- Create Activity Modal -->
    <div class="modal-overlay" id="create-activity-modal" style="display:none">
      <div class="modal">
        <h3>Assign Work to Agent</h3>
        <form id="create-activity-form" onsubmit="doCreateActivity(event)">
          <div class="form-group">
            <label>Assign To *</label>
            <select id="act-agent" required>
              <option value="">Select agent...</option>
              {agent_options}
            </select>
            <p class="form-hint">The agent session that should pick up this task.</p>
          </div>
          <div class="form-group">
            <label>Task *</label>
            <textarea id="act-task" rows="3" required placeholder="What should the agent do?"></textarea>
            <p class="form-hint">Write the task the same way you would hand it to a person.</p>
          </div>
          <div class="form-group">
            <label>Workspace / Scope</label>
            <select id="act-memory-scope">
              <option value="">Agent private scope (agent only, no workspace)</option>
              {activity_scope_options}
            </select>
            <p class="form-hint">The agent picks up work that matches its authorized scopes. Set this to a workspace if the agent is configured with workspace access — the agent session will only claim tasks in scopes it can read.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-activity-modal')">Cancel</button>
            <button type="submit" class="btn">Assign</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Reassign Activity Modal -->
    <div class="modal-overlay" id="reassign-modal" style="display:none">
      <div class="modal">
        <h3>Reassign Activity</h3>
        <div class="form-group">
          <label>New Agent</label>
          <select id="reassign-agent-select" required>
            <option value="">Select agent...</option>
            {reassign_options}
          </select>
          <p class="form-hint">Choose the agent to take over this activity.</p>
        </div>
        <input type="hidden" id="reassign-activity-id" value="">
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" onclick="closeModal('reassign-modal')">Cancel</button>
          <button type="button" class="btn" onclick="doReassign()">Reassign</button>
        </div>
      </div>
    </div>

    <div class="modal-overlay" id="prune-modal" style="display:none">
      <div class="modal">
        <h3 id="prune-modal-title">Prune Data</h3>
        <form id="prune-form" onsubmit="submitPrune(event)">
          <input type="hidden" id="prune-resource-type" value="">
          <div class="form-group">
            <label>Delete records older than *</label>
            <input type="date" id="prune-before-date" required>
            <p class="form-hint">This is a manual maintenance action. It only deletes terminal activity records older than the selected date; active tasks are left alone.</p>
          </div>
          <div id="prune-result" style="margin-top:8px"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('prune-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Prune</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Briefing Modal -->
    <div class="modal-overlay" id="briefing-modal" style="display:none">
      <div class="modal" style="max-width:600px">
  <h3>Briefing</h3>
        <div id="briefing-content" style="max-height:400px;overflow-y:auto"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('briefing-modal')">Close</button>
        </div>
      </div>
    </div>
    """,
        "/activity",
        js,
        session=session,
    )

