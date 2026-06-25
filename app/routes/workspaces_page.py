"""Workspaces (projects) dashboard page. Split from dashboard.py — see
private/dashboard-split-plan.md."""

from fastapi import APIRouter, Request, Depends

from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    local_dt,
    get_icon,
)

router = APIRouter()


@router.get("/workspaces")
async def workspaces_page(request: Request, session: dict = Depends(require_auth)):
    import json
    from app.services import workspace_service, agent_service

    workspaces = (
        workspace_service.list_workspaces()
        if session.get("role") == "admin"
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )
    all_agents = (
        agent_service.list_agents()
        if session.get("role") == "admin"
        else agent_service.list_agents(owner_user_id=session["user_id"])
    )
    workspace_agent_access_data = [
        {
            "id": a["id"],
            "display_name": a.get("display_name") or a["id"],
            "owner_user_id": a.get("owner_user_id") or "",
            "default_user_id": a.get("default_user_id") or "",
            "is_active": bool(a.get("is_active")),
            "read_scopes": agent_service.parse_scopes(a.get("read_scopes_json", "[]")),
            "write_scopes": agent_service.parse_scopes(a.get("write_scopes_json", "[]")),
        }
        for a in all_agents
    ]

    project_agent_access = {}
    for p in workspaces:
        pscope = f"workspace:{p['id']}"
        read_agents = [
            a["id"]
            for a in all_agents
            if pscope in (json.loads(a.get("read_scopes_json", "[]")) or [])
        ]
        write_agents = [
            a["id"]
            for a in all_agents
            if pscope in (json.loads(a.get("write_scopes_json", "[]")) or [])
        ]
        project_agent_access[p["id"]] = (read_agents, write_agents)

    def workspace_row(p):
        read_tags = "".join(
            f"<span class='scope-tag' title='Read'>{a}</span>"
            for a in project_agent_access[p["id"]][0]
        )
        write_tags = "".join(
            f"<span class='scope-tag scope-write' title='Write'>{a}</span>"
            for a in project_agent_access[p["id"]][1]
        )
        read_tags = read_tags or "<span class='text-muted'>none</span>"
        write_tags = write_tags or "<span class='text-muted'>none</span>"
        is_active = p.get("is_active")
        active_label = "active" if is_active else "inactive"
        if is_active:
            toggle_btn = f"<button type='button' class='btn btn-sm btn-warning' data-workspace-action='deactivate' data-workspace-id='{p['id']}'>Deactivate</button>"
        else:
            toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' data-workspace-action='reactivate' data-workspace-id='{p['id']}'>Reactivate</button>"
        return (
            f"<tr>"
            f"<td><code>{p['id']}</code></td>"
            f"<td>{p.get('name', '')}</td>"
            f"<td><span class='badge badge-{active_label}'>{active_label}</span></td>"
            f"<td>{p.get('owner_user_id', '')}</td>"
            f"<td class='agent-access-cell'>"
            f"<div class='agent-read-list'><span class='access-label'>Read</span>{read_tags}</div>"
            f"<div class='agent-write-list'><span class='access-label'>Write</span>{write_tags}</div>"
            f"</td>"
            f"<td>{local_dt(p.get('created_at'), style='date')}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' data-workspace-action='edit' data-workspace-id='{p['id']}'>Edit</button>"
            f"{toggle_btn}"
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-workspace-action='purge' data-workspace-id='{p['id']}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            f"</div></td></tr>"
        )

    rows = "".join(workspace_row(p) for p in workspaces)

    js = """
    <script>
    const WORKSPACE_AGENT_STATE = Object.fromEntries(
      __WORKSPACE_AGENT_STATE__.map(function(agent) { return [agent.id, agent]; })
    );

    async function refreshProjects() { location.reload(); }
    async function deactivateProject(id) {
      if (!confirm('Deactivate this workspace?')) return;
      const j = await apiFetch('/api/workspaces/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('Workspace deactivated'); refreshProjects(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function reactivateProject(id) {
      if (!confirm('Reactivate this workspace?')) return;
      const j = await apiFetch('/api/workspaces/' + id + '/activate', { method: 'POST' });
      if (j.ok) { showToast('Workspace reactivated'); refreshProjects(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function purgeProject(id) {
      if (!confirm('PERMANENTLY DELETE this workspace? This cannot be undone.')) return;
      const j = await apiFetch('/api/workspaces/' + id + '/purge', { method: 'POST' });
      if (j.ok) { 
        showToast('Workspace deleted', 'success'); 
        refreshProjects(); 
      } else { 
        showToast(j.error?.message || 'Failed to delete workspace', 'danger'); 
      }
    }
    async function editProject(id) {
      openModal('edit-workspace-modal');
      const idEl = document.getElementById('ep-id');
      const nameEl = document.getElementById('ep-name');
      const descriptionEl = document.getElementById('ep-description');
      const collaboratorsEl = document.getElementById('ep-collaborators');
      const agentAccessEl = document.getElementById('ep-agent-access');
      idEl.textContent = id;
      nameEl.value = '';
      descriptionEl.value = '';
      if (collaboratorsEl) {
        collaboratorsEl.innerHTML = '<div class="text-muted">Loading collaborators...</div>';
      }
      if (agentAccessEl) {
        agentAccessEl.innerHTML = '<div class="text-muted">Loading workspace details...</div>';
      }
      try {
        const r = await fetch('/api/workspaces/' + id);
        const j = await r.json();
        if (!j.ok) { throw new Error(j.error?.message || 'Error loading workspace'); }
        const p = j.data.workspace;
        idEl.textContent = p.id;
        nameEl.value = p.name || '';
        descriptionEl.value = p.description || '';
        renderWorkspaceCollaborators(p.id);
        renderWorkspaceAgentAccess(p.id);
      } catch (err) {
        if (collaboratorsEl) {
          collaboratorsEl.innerHTML = '<div class="alert alert-danger">Unable to load collaborators.</div>';
        }
        if (agentAccessEl) {
          agentAccessEl.innerHTML = '<div class="alert alert-danger">Unable to load workspace details.</div>';
        }
        showToast(err.message || 'Error loading workspace', 'danger');
      }
    }
    async function submitEditProject(e) {
      e.preventDefault();
      const id = document.getElementById('ep-id').textContent;
      const body = {
        name: document.getElementById('ep-name').value,
        description: document.getElementById('ep-description').value,
      };
      const j = await apiFetch('/api/workspaces/' + id, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) { showToast('Workspace updated'); closeModal('edit-workspace-modal'); refreshProjects(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }

    async function renderWorkspaceCollaborators(workspaceId) {
      const container = document.getElementById('ep-collaborators');
      if (!container) return;
      container.innerHTML = '<div class="text-muted">Loading collaborators...</div>';
      try {
        const r = await fetch('/api/workspaces/' + workspaceId + '/collaborators');
        const j = await r.json();
        if (!j.ok) {
          if (r.status === 403) {
            container.innerHTML = '<div class="text-muted">Only the workspace owner or an admin can manage collaborators.</div>';
            return;
          }
          throw new Error(j.error?.message || 'Error loading collaborators');
        }
        const collaborators = j.data?.collaborators || [];
        const rows = collaborators.map(function(c) {
          const canRemove = c.role !== 'owner';
          const removeButton = canRemove
            ? '<button type="button" class="btn btn-sm btn-danger icon-delete-btn" data-workspace-collaborator-remove="true" data-workspace-id="' + escapeHtml(workspaceId) + '" data-user-id="' + escapeHtml(c.user_id || '') + '" title="Remove collaborator" aria-label="Remove collaborator">' + get_icon('delete') + '</button>'
            : '<span class="text-muted">owner</span>';
          return '<tr>' +
            '<td><code>' + escapeHtml(c.user_id || '') + '</code></td>' +
            '<td>' + (c.can_read ? 'Yes' : 'No') + '</td>' +
            '<td>' + (c.can_write ? 'Yes' : 'No') + '</td>' +
            '<td>' + removeButton + '</td>' +
          '</tr>';
        }).join('');
        container.innerHTML =
          '<div class="section-header" style="margin-bottom:8px">' +
            '<h3>Collaborators</h3>' +
            '<div class="section-note">Share this workspace with specific users. Their agents can use workspace scopes while access remains granted.</div>' +
          '</div>' +
          '<div class="card" style="padding:12px;margin-bottom:12px">' +
            '<form data-workspace-collaborator-form="true" data-workspace-id="' + escapeHtml(workspaceId) + '">' +
              '<div class="form-row" style="display:grid;grid-template-columns:1.4fr .7fr .7fr auto;gap:8px;align-items:end">' +
                '<div class="form-group" style="margin:0"><label>User ID</label><input type="text" name="user_id" placeholder="e.g. brian" autocomplete="off"></div>' +
                '<label class="checkbox-label" style="margin:0"><input type="checkbox" name="can_read" checked> Read</label>' +
                '<label class="checkbox-label" style="margin:0"><input type="checkbox" name="can_write"> Write</label>' +
                '<button type="submit" class="btn">Add</button>' +
              '</div>' +
            '</form>' +
          '</div>' +
          '<table><thead><tr><th>User</th><th>Read</th><th>Write</th><th class="actions-cell">Actions</th></tr></thead><tbody>' +
            (rows || '<tr><td colspan="4" class="empty">No collaborators yet.</td></tr>') +
          '</tbody></table>';
      } catch (err) {
        container.innerHTML = '<div class="alert alert-danger">' + escapeHtml(err.message || 'Error loading collaborators') + '</div>';
      }
    }

    async function addWorkspaceCollaborator(e, workspaceId) {
      e.preventDefault();
      const form = e.target;
      const data = new FormData(form);
      const userId = (data.get('user_id') || '').toString().trim();
      if (!userId) {
        showToast('User ID is required to add a collaborator', 'danger');
        return false;
      }
      const body = {
        can_read: data.get('can_read') ? true : false,
        can_write: data.get('can_write') ? true : false,
      };
      const j = await apiFetch('/api/workspaces/' + workspaceId + '/collaborators/' + encodeURIComponent(userId), {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      if (j.ok) {
        showToast('Collaborator updated');
        renderWorkspaceCollaborators(workspaceId);
      } else {
        showToast(j.error?.message || 'Failed', 'danger');
      }
      return false;
    }

    async function removeWorkspaceCollaborator(workspaceId, userId) {
      if (!confirm('Remove this collaborator?')) return;
      const j = await apiFetch('/api/workspaces/' + workspaceId + '/collaborators/' + encodeURIComponent(userId), {
        method: 'DELETE',
      });
      if (j.ok) {
        showToast('Collaborator removed');
        renderWorkspaceCollaborators(workspaceId);
      } else {
        showToast(j.error?.message || 'Failed', 'danger');
      }
    }

    document.addEventListener('submit', function(e) {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.dataset.workspaceCollaboratorForm !== 'true') return;
      const workspaceId = form.dataset.workspaceId;
      if (!workspaceId) return;
      addWorkspaceCollaborator(e, workspaceId);
    });

    document.addEventListener('click', function(e) {
      const button = e.target.closest('button[data-workspace-collaborator-remove="true"]');
      if (!button) return;
      const workspaceId = button.dataset.workspaceId;
      const userId = button.dataset.userId;
      if (!workspaceId || !userId) return;
      removeWorkspaceCollaborator(workspaceId, userId);
    });

    function renderWorkspaceAgentAccess(workspaceId) {
      const scope = 'workspace:' + workspaceId;
      const agents = Object.values(WORKSPACE_AGENT_STATE);
      const container = document.getElementById('ep-agent-access');
      if (!container) return;
      container.replaceChildren();

      const header = document.createElement('div');
      header.className = 'section-header';
      header.style.marginBottom = '8px';

      const title = document.createElement('h3');
      title.textContent = 'Agent Access';
      header.appendChild(title);

      const note = document.createElement('div');
      note.className = 'section-note';
      note.textContent = 'This is a convenience editor. Toggling a checkbox updates the selected agent workspace scopes.';
      header.appendChild(note);
      container.appendChild(header);

      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      ['Agent', 'Owner', 'Status', 'Read', 'Write', 'State'].forEach(function(label) {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      table.appendChild(thead);

      const tbody = document.createElement('tbody');
      if (!agents.length) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.className = 'empty';
        td.textContent = 'No agents available.';
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        agents.forEach(function(agent) {
          const readEnabled = (agent.read_scopes || []).includes(scope);
          const writeEnabled = (agent.write_scopes || []).includes(scope);
          const tr = document.createElement('tr');
          tr.dataset.agentId = agent.id;

          const agentTd = document.createElement('td');
          const strong = document.createElement('strong');
          strong.textContent = agent.display_name || agent.id;
          const br = document.createElement('br');
          const code = document.createElement('code');
          code.textContent = agent.id;
          agentTd.appendChild(strong);
          agentTd.appendChild(br);
          agentTd.appendChild(code);
          tr.appendChild(agentTd);

          const ownerTd = document.createElement('td');
          ownerTd.textContent = agent.owner_user_id || '-';
          tr.appendChild(ownerTd);

          const statusTd = document.createElement('td');
          const statusBadge = document.createElement('span');
          statusBadge.className = 'badge ' + (agent.is_active ? 'badge-active' : 'badge-inactive');
          statusBadge.textContent = agent.is_active ? 'active' : 'inactive';
          statusTd.appendChild(statusBadge);
          tr.appendChild(statusTd);

          function makeScopeCell(kind, checked, labelText) {
            const td = document.createElement('td');
            const label = document.createElement('label');
            label.className = 'checkbox-label';
            label.style.margin = '0';
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.dataset.scopeKind = kind;
            input.checked = checked;
            const span = document.createElement('span');
            span.textContent = labelText;
            label.appendChild(input);
            label.appendChild(document.createTextNode(' '));
            label.appendChild(span);
            td.appendChild(label);
            return td;
          }

          tr.appendChild(makeScopeCell('read', readEnabled, 'Read'));
          tr.appendChild(makeScopeCell('write', writeEnabled, 'Write'));

          const stateTd = document.createElement('td');
          stateTd.className = 'agent-scope-status text-muted';
          tr.appendChild(stateTd);

          tbody.appendChild(tr);
        });
      }

      table.appendChild(tbody);
      container.appendChild(table);
    }

    async function toggleWorkspaceAgentScope(agentId, workspaceId, kind, enabled, checkbox) {
      const agent = WORKSPACE_AGENT_STATE[agentId];
      if (!agent) return;
      const scope = 'workspace:' + workspaceId;
      const prior = {
        read: (agent.read_scopes || []).slice(),
        write: (agent.write_scopes || []).slice(),
      };
      const scopes = kind === 'read' ? agent.read_scopes : agent.write_scopes;
      const idx = scopes.indexOf(scope);
      if (enabled && idx === -1) {
        scopes.push(scope);
      } else if (!enabled && idx !== -1) {
        scopes.splice(idx, 1);
      }

      const row = document.querySelector('[data-agent-id="' + CSS.escape(agentId) + '"]');
      const stateCell = row ? row.querySelector('.agent-scope-status') : null;
      const controls = row ? row.querySelectorAll('input[type="checkbox"]') : [];
      controls.forEach(function(input) { input.disabled = true; });
      if (stateCell) stateCell.textContent = 'Saving...';

      const body = {
        read_scopes: agent.read_scopes,
        write_scopes: agent.write_scopes,
      };
      const j = await apiFetch('/api/agents/' + agentId, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) {
        if (stateCell) stateCell.textContent = 'Saved';
        setTimeout(function() {
          if (stateCell && stateCell.textContent === 'Saved') stateCell.textContent = '';
        }, 1000);
      } else {
        agent.read_scopes = prior.read;
        agent.write_scopes = prior.write;
        if (checkbox) checkbox.checked = enabled ? false : true;
        if (stateCell) stateCell.textContent = 'Error';
        showToast(j.error?.message || 'Failed to update agent access', 'danger');
      }
      controls.forEach(function(input) { input.disabled = false; });
    }

    document.addEventListener('change', function(e) {
      const target = e.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (target.type !== 'checkbox') return;
      const scopeKind = target.dataset.scopeKind;
      const row = target.closest('[data-agent-id]');
      if (!scopeKind || !row) return;
      const agentId = row.dataset.agentId;
      const workspaceId = document.getElementById('ep-id').textContent;
      if (!workspaceId) return;
      toggleWorkspaceAgentScope(agentId, workspaceId, scopeKind, target.checked, target);
    });

    document.addEventListener('click', function(e) {
      const button = e.target.closest('button[data-workspace-action]');
      if (!button) return;
      const action = button.dataset.workspaceAction;
      const workspaceId = button.dataset.workspaceId;
      if (!action || !workspaceId) return;
      if (action === 'edit') editProject(workspaceId);
      else if (action === 'deactivate') deactivateProject(workspaceId);
      else if (action === 'reactivate') reactivateProject(workspaceId);
      else if (action === 'purge') purgeProject(workspaceId);
    });
    </script>""".replace(
        "__WORKSPACE_AGENT_STATE__",
        json.dumps(workspace_agent_access_data).replace("</", "<\\/"),
    )
    return render_page(
        "Workspaces",
        f"""
    <div class="page-header"><h1>Workspaces</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-workspace-modal')">+ Create Workspace</button>
    </div></div>
    <div class="card">
      <div class="section-header">
        <h3>Workspaces</h3>
        <div class="section-note">Shared scopes for collaboration. Assign access from Agents → Edit, or use the workspace editor here as a convenience. Scope names stay <code>workspace:&lt;id&gt;</code>.</div>
      </div>
      <table><thead><tr><th>Scope</th><th>Name</th><th>Status</th><th>Owner</th><th>Agents (Read/Write)</th><th>Created</th><th class="actions-cell">Actions</th></tr></thead>
        <tbody>{rows or "<tr><td colspan=7 class=empty>No workspaces yet.</td></tr>"}</tbody></table>
    </div>

    <!-- Create Workspace Modal -->
    <div class="modal-overlay" id="create-workspace-modal" style="display:none">
      <div class="modal">
        <h3>Create Workspace</h3>
        <form id="create-workspace-form" onsubmit="createProject(event)">
          <div class="form-group">
            <label>Workspace ID *</label>
            <input type="text" id="cp-id" pattern="[a-z0-9_\\-]+" placeholder="e.g. myproject" required>
            <p class="form-hint">Lowercase letters, numbers, hyphens, underscores only.</p>
          </div>
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="cp-name" required>
          </div>
          <div class="form-group">
            <label>Description</label>
            <textarea id="cp-description" rows="2"></textarea>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-workspace-modal')">Cancel</button>
            <button type="submit" class="btn">Create</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Edit Workspace Modal -->
    <div class="modal-overlay" id="edit-workspace-modal" style="display:none">
      <div class="modal">
        <h3>Edit Workspace: <span id="ep-id"></span></h3>
        <form id="edit-workspace-form" onsubmit="submitEditProject(event)">
          <div class="form-group">
            <label>Name</label>
            <input type="text" id="ep-name">
          </div>
          <div class="form-group">
            <label>Description</label>
            <textarea id="ep-description" rows="2"></textarea>
          </div>
          <div class="form-group">
            <div id="ep-collaborators"></div>
          </div>
          <div class="form-group">
            <div id="ep-agent-access"></div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-workspace-modal')">Cancel</button>
            <button type="submit" class="btn">Save</button>
          </div>
        </form>
      </div>
    </div>
    """
        + """
    <script>
    async function createProject(e) {
      e.preventDefault();
      const body = {
        id: document.getElementById('cp-id').value,
        name: document.getElementById('cp-name').value,
        description: document.getElementById('cp-description').value || '',
      };
      const j = await apiFetch('/api/workspaces', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) {
        closeModal('create-workspace-modal');
        document.getElementById('create-workspace-form').reset();
        showToast('Workspace created');
        refreshProjects();
      } else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    </script>""",
        "/workspaces",
        js,
        session=session,
    )


