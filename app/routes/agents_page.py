"""Agents dashboard page. Split from dashboard.py — see
private/dashboard-split-plan.md."""

import json

from fastapi import APIRouter, Request, Depends

from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    local_dt,
    get_icon,
)

router = APIRouter()


@router.get("/agents")
async def agents_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import agent_service, workspace_service

    is_admin = session.get("role") == "admin"
    agents = (
        agent_service.list_agents()
        if is_admin
        else agent_service.list_visible_agents(session["user_id"])
    )
    all_workspaces = (
        workspace_service.list_workspaces()
        if is_admin
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )

    def build_scope_list(prefix):
        h = f'<div class="scope-selectors" id="{prefix}-scopes">'
        h += '<label class="checkbox-label" data-scope-row="shared"><input type="checkbox" data-scope="shared"> <span>Other users can use this scope</span></label>'
        if prefix.endswith("read"):
            h += (
                f'<label class="checkbox-label" data-scope-row="user:{session["user_id"]}">'
                f'<input type="checkbox" data-scope="user:{session["user_id"]}" checked disabled data-required-scope="true">'
                f' <span>User <code>user:{session["user_id"]}</code> (owner context)</span></label>'
            )
        elif prefix.endswith("recall"):
            # Recall set may include the owner scope, but it is selectable (not required).
            h += (
                f'<label class="checkbox-label" data-scope-row="user:{session["user_id"]}">'
                f'<input type="checkbox" data-scope="user:{session["user_id"]}">'
                f' <span>User <code>user:{session["user_id"]}</code> (owner context)</span></label>'
            )
        if all_workspaces:
            h += "<h4>Workspaces</h4>"
            for p in all_workspaces:
                h += f'<label class="checkbox-label" data-scope-row="workspace:{p["id"]}"><input type="checkbox" data-scope="workspace:{p["id"]}"> <span>{p["name"]} <code>workspace:{p["id"]}</code></span></label>'
        if agents:
            h += "<h4>Other Agent Private Scopes</h4>"
            for a in agents:
                h += f'<label class="checkbox-label" data-scope-row="agent:{a["id"]}"><input type="checkbox" data-scope="agent:{a["id"]}"> <span>{a["display_name"]} ({a["id"]})</span></label>'
        h += "</div>"
        return h

    ca_read_html = build_scope_list("ca-read")
    ca_write_html = build_scope_list("ca-write")
    ca_recall_html = build_scope_list("ca-recall")
    edit_read_html = build_scope_list("edit-read")
    edit_write_html = build_scope_list("edit-write")
    edit_recall_html = build_scope_list("edit-recall")

    def agent_row(a):
        active = a.get("is_active")
        status_badge = f"<span class='badge badge-{'active' if active else 'inactive'}'>{'active' if active else 'inactive'}</span>"
        read_scopes = agent_service.parse_scopes(a.get("read_scopes_json", "[]"))
        write_scopes = agent_service.parse_scopes(a.get("write_scopes_json", "[]"))
        own_scope = f"agent:{a['id']}"
        read_extra = [s for s in read_scopes if s != own_scope]
        write_extra = [s for s in write_scopes if s != own_scope]
        shared_badge = (
            "<span class='badge badge-info'>Shared</span>"
            if a.get("is_shared") or agent_service.is_agent_shared(a)
            else ""
        )
        access_summary = (
            f"<span class='scope-tag' title='Implicit private scope'>{own_scope}</span>"
            f"{shared_badge}"
            f"<span class='text-muted'> + {len(read_extra)} read / {len(write_extra)} write grants</span>"
        )
        can_manage = is_admin or a.get("owner_user_id") == session["user_id"]
        if can_manage:
            if active:
                toggle_btn = f"<button type='button' class='btn btn-sm btn-warning' onclick=\"deactivateAgent('{a['id']}')\">Deactivate</button>"
            else:
                toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"reactivateAgent('{a['id']}')\">Reactivate</button>"
        else:
            toggle_btn = ""
        owner_id = a.get("owner_user_id", "")
        default_user_id = a.get("default_user_id", "") or owner_id
        if can_manage:
            action_buttons = (
                f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"editAgent('{a['id']}')\">Edit</button>"
                f"<a class='btn btn-sm btn-secondary' href='/integrations?agent_id={a['id']}'>Integrations</a>"
                f"{toggle_btn}"
                f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick=\"purgeAgent('{a['id']}')\" title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            )
        else:
            action_buttons = (
                f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"viewAgent('{a['id']}')\">View</button>"
            )
        return (
            f"<tr>"
            f"<td><strong>{a.get('display_name', '')}</strong><br><code>{a['id']}</code>"
            f"<div class='text-muted'>Owner: {owner_id} · Default user: {default_user_id}</div></td>"
            f"<td>{status_badge}</td>"
            f"<td>{access_summary}</td>"
            f"<td>{local_dt(a.get('created_at'), style='date')}</td>"
            f"<td><div class='actions-cell'>"
            f"{action_buttons}"
            f"</div></td></tr>"
        )

    rows = "".join(agent_row(a) for a in agents)

    js = """
    <script>
    const IS_ADMIN = __IS_ADMIN__;
    const CURRENT_USER_ID = __CURRENT_USER_ID__;
    let agentModalReadOnly = false;
    async function refreshAgents() { location.reload(); }
    async function deactivateAgent(id) {
      if (!confirm('Deactivate this agent? It will no longer be able to authenticate.')) return;
      const j = await apiFetch('/api/agents/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('Agent deactivated'); refreshAgents(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function reactivateAgent(id) {
      if (!confirm('Reactivate this agent?')) return;
      const j = await apiFetch('/api/agents/' + id + '/activate', { method: 'POST' });
      if (j.ok) { showToast('Agent reactivated'); refreshAgents(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function purgeAgent(id) {
      if (!confirm('PERMANENTLY DELETE this agent? This cannot be undone.')) return;
      const j = await apiFetch('/api/agents/' + id + '/purge', { method: 'POST' });
      if (j.ok) { 
        showToast('Agent deleted', 'success'); 
        refreshAgents(); 
      } else { 
        showToast(j.error?.message || 'Failed to delete agent', 'danger'); 
      }
    }
    async function rotateAgentKey(id) {
      const j = await apiFetch('/api/agents/' + id + '/rotate_key', { method: 'POST' });
      if (j.ok) {
        document.getElementById('rotate-modal-body').innerHTML =
          '<div class="alert alert-danger"><strong>Save this key now - it will not be shown again!</strong></div>' +
          '<div class="api-key-display"><code>' + j.data.api_key + '</code></div>' +
          '<button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(j.data.api_key) + ', this)">Copy</button>';
        openModal('rotate-modal');
      } else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    function setAgentModalReadOnly(readOnly) {
      agentModalReadOnly = Boolean(readOnly);
      const saveButton = document.getElementById('edit-agent-save');
      const modalTitle = document.getElementById('edit-agent-modal-title');
      const form = document.getElementById('edit-agent-form');
      if (modalTitle) {
        modalTitle.textContent = readOnly ? 'View Agent' : 'Edit Agent';
      }
      if (saveButton) {
        saveButton.style.display = readOnly ? 'none' : '';
      }
      if (form) {
        form.querySelectorAll('input, select, textarea').forEach(input => {
          if (input.type === 'hidden') return;
          if (input.id === 'edit-agent-id') return;
          if (input.id === 'edit-display-name' || input.id === 'edit-description') {
            input.disabled = readOnly;
          }
        });
      }
    }
    async function editAgent(id) {
      return openAgentModal(id, false);
    }
    async function viewAgent(id) {
      return openAgentModal(id, true);
    }
    async function openAgentModal(id, readOnly) {
      const j = await apiFetch('/api/agents/' + id, { method: 'GET' });
      if (!j.ok) { showToast(j.error.message || 'Error', 'danger'); return; }
      const a = j.data.agent;
      document.getElementById('edit-agent-id').textContent = a.id;
      document.getElementById('edit-display-name').value = a.display_name || '';
      document.getElementById('edit-description').value = a.description || '';
      document.getElementById('edit-owner').textContent = a.owner_user_id || '-';
      document.getElementById('edit-default-user').textContent = a.default_user_id || a.owner_user_id || '-';
      const readScopes = JSON.parse(a.read_scopes_json || '[]');
      const writeScopes = JSON.parse(a.write_scopes_json || '[]');
      setSelectedScopes('edit-read-scopes', readScopes);
      setSelectedScopes('edit-write-scopes', writeScopes);
      // Default recall: null column => "all readable" (Option A); else the stored subset.
      const recall = a.default_recall_scopes_json ? JSON.parse(a.default_recall_scopes_json) : null;
      const recallAll = document.getElementById('edit-recall-all');
      if (recall === null) {
        // Never configured: box checked (= all read). Underneath, start blank —
        // only the locked own-scope — so unchecking shows a clean slate, not a
        // pre-filled suggestion that looks like an existing default.
        if (recallAll) recallAll.checked = true;
        setSelectedScopes('edit-recall-scopes', []);
      } else {
        if (recallAll) recallAll.checked = false;
        setSelectedScopes('edit-recall-scopes', recall);
      }
      if (recallAll) recallAll.disabled = Boolean(readOnly);
      toggleRecallPicker('edit');
      setAgentModalReadOnly(Boolean(readOnly));
      // Hide and disable the agent's own scope row: self-access is implicit.
      ['edit-read-scopes', 'edit-write-scopes', 'edit-recall-scopes'].forEach(containerId => {
        const isRecall = containerId === 'edit-recall-scopes';
        document.querySelectorAll('#' + containerId + ' input').forEach(input => {
          const isOwnScope = input.dataset.scope === 'agent:' + a.id;
          input.disabled = Boolean(readOnly) || isOwnScope || input.dataset.requiredScope === 'true';
          const label = input.closest('label');
          if (!label) return;
          if (isOwnScope && isRecall) {
            // Show the agent's own scope as the always-on recall baseline:
            // visible, checked, locked — so the floor is obvious, not hidden.
            input.checked = true;
            label.hidden = false;
            label.classList.add('implicit-own-scope');
            if (!label.dataset.alwaysTag) {
              label.insertAdjacentHTML('beforeend', ' <span class="text-muted">(always included)</span>');
              label.dataset.alwaysTag = '1';
            }
          } else {
            label.hidden = isOwnScope;
            label.classList.toggle('implicit-own-scope', isOwnScope);
          }
        });
      });
      openModal('edit-agent-modal');
    }
      function getSelectedScopes(containerId) {
        return Array.from(document.querySelectorAll('#' + containerId + ' input:checked')).map(i => i.dataset.scope);
      }
      function setSelectedScopes(containerId, scopes) {
        document.querySelectorAll('#' + containerId + ' input').forEach(i => {
          i.checked = scopes.includes(i.dataset.scope);
        });
      }
      function toggleRecallPicker(prefix) {
        const all = document.getElementById(prefix + '-recall-all');
        const wrap = document.getElementById(prefix + '-recall-wrap');
        if (wrap) wrap.style.display = (all && all.checked) ? 'none' : '';
      }
      function normalizeAgentId(value) {
        return (value || '').trim().toLowerCase();
      }

      async function submitEditAgent(e) {
      e.preventDefault();
      if (agentModalReadOnly) return;
      const id = document.getElementById('edit-agent-id').textContent;
        const ownScope = 'agent:' + id;
        const userScope = 'user:' + CURRENT_USER_ID;
        const body = {
          display_name: document.getElementById('edit-display-name').value,
          description: document.getElementById('edit-description').value,
          read_scopes: getSelectedScopes('edit-read-scopes'),
          write_scopes: getSelectedScopes('edit-write-scopes'),
        };
        body.read_scopes.push(ownScope);
        body.write_scopes.push(ownScope);
        if (!body.read_scopes.includes(userScope)) body.read_scopes.push(userScope);
        const editRecallAll = document.getElementById('edit-recall-all');
        if (editRecallAll && editRecallAll.checked) {
          body.reset_default_recall_scopes = true;
        } else {
          const recall = getSelectedScopes('edit-recall-scopes');
          recall.push(ownScope);
          body.default_recall_scopes = recall;
        }
        const j = await apiFetch('/api/agents/' + id, { method: 'PUT', body: JSON.stringify(body) });
        if (j.ok) { showToast('Agent updated'); closeModal('edit-agent-modal'); refreshAgents(); }
        else { showToast(j.error.message || 'Failed', 'danger'); }
      }

      async function createAgent(e) {
        e.preventDefault();
        const errorBox = document.getElementById('create-agent-error');
          if (errorBox) {
            errorBox.style.display = 'none';
            errorBox.textContent = '';
          }
        const agentId = normalizeAgentId(document.getElementById('ca-id').value);
        const body = {
          id: agentId,
          display_name: document.getElementById('ca-display-name').value.trim(),
          description: document.getElementById('ca-description').value.trim(),
          read_scopes: getSelectedScopes('ca-read-scopes'),
          write_scopes: getSelectedScopes('ca-write-scopes'),
        };
        // Ensure private scope is added if not present (it's implicit in backend but good to show)
        const privateScope = 'agent:' + agentId;
        const userScope = 'user:' + CURRENT_USER_ID;
        if (!body.read_scopes.includes(privateScope)) body.read_scopes.push(privateScope);
        if (!body.write_scopes.includes(privateScope)) body.write_scopes.push(privateScope);
        if (!body.read_scopes.includes(userScope)) body.read_scopes.push(userScope);
        const caRecallAll = document.getElementById('ca-recall-all');
        if (caRecallAll && !caRecallAll.checked) {
          const recall = getSelectedScopes('ca-recall-scopes');
          recall.push(privateScope);
          body.default_recall_scopes = recall;
        }

        try {
          const j = await apiFetch('/api/agents', { method: 'POST', body: JSON.stringify(body) });
          if (j.ok) {
            showToast('Agent created');
            closeModal('create-agent-modal');
            document.getElementById('rotate-modal-body').innerHTML =
              '<div class="alert alert-success">Agent created successfully.</div>' +
              '<p class="text-muted">Generate a one-time connection key and tool config from Integrations when you are ready to connect this agent.</p>' +
              '<a class="btn" href="/integrations?agent_id=' + encodeURIComponent(j.data.agent.id) + '">Go to Integrations</a>';
            openModal('rotate-modal');
          } else {
            const message = j.error?.message || 'Failed to create agent';
            if (errorBox) {
              errorBox.textContent = message;
              errorBox.style.display = 'block';
            }
            showToast(message, 'danger');
          }
        } catch (err) {
          const message = err?.message || 'Failed to create agent';
          if (errorBox) {
            errorBox.textContent = message;
            errorBox.style.display = 'block';
          }
          showToast(message, 'danger');
        }
      }
    </script>"""
    js = js.replace("__IS_ADMIN__", str(is_admin).lower())
    js = js.replace("__CURRENT_USER_ID__", json.dumps(session["user_id"]))

    return render_page(
        "Agents",
        f"""
    <div class="page-header"><h1>Agents</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-agent-modal')">+ Create Agent</button>
    </div></div>
    <div class="card">
      <h3>Agent Access</h3>
      <p class="text-muted access-summary">Agents belong to one owner/default user. The shared/global option grants other users access to the scope itself. Use workspaces as shared collaboration spaces; personal user scopes stay tied to the agent owner.</p>
    </div>

    <div class="card">
      <h3>All Agents</h3>
<table><thead><tr><th>Agent</th><th>Status</th><th>Access</th><th>Created</th><th class="actions-cell">Actions</th></tr></thead>
        <tbody>{rows or "<tr><td colspan=5 class=empty>No agents yet.</td></tr>"}</tbody></table>
    </div>

    <!-- Create Agent Modal -->
    <div class="modal-overlay" id="create-agent-modal" style="display:none">
      <div class="modal">
        <h3>Create Agent</h3>
        <form id="create-agent-form" onsubmit="createAgent(event)">
          <div class="form-group">
            <label>Agent ID *</label>
            <input type="text" id="ca-id" pattern="[a-z0-9_\\-]+" placeholder="e.g. coding-agent" required>
            <p class="form-hint">Lowercase letters, numbers, hyphens, underscores only.</p>
          </div>
          <div class="form-group">
            <label>Display Name *</label>
            <input type="text" id="ca-display-name" required>
          </div>
          <div class="form-group">
            <label>Description</label>
            <input type="text" id="ca-description">
          </div>
          <div class="form-group">
            <label>Can Read From</label>
            {ca_read_html}
            <p class="form-hint">User context is automatic for the owner/default user. Use this area for workspace, shared/global, or other agent-private context outside the agent's own scope.</p>
          </div>
          <div class="form-group">
            <label>Can Write To</label>
            {ca_write_html}
            <p class="form-hint">Grant write access only where the agent should be allowed to save new memory or credentials. Use workspace scopes for multi-user collaboration. User write is available only as an explicit advanced choice.</p>
          </div>
          <div class="form-group">
            <label>Default Recall Scopes</label>
            <label class="checkbox-label"><input type="checkbox" id="ca-recall-all" checked onchange="toggleRecallPicker('ca')"> <span>Recall from <strong>all</strong> scopes this agent can read</span></label>
            <div id="ca-recall-wrap" style="display:none">
              {ca_recall_html}
              <p class="form-hint"><strong>Checked</strong> = recall from everything the agent can read (the default — same as before this setting existed). <strong>Uncheck</strong> to recall only the scopes ticked below. The agent's own scope is always included, and any other readable scope stays reachable on demand via <code>memory_search(scope=…)</code>.</p>
            </div>
          </div>
          <div id="create-agent-error" class="alert alert-danger" style="display:none"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-agent-modal')">Cancel</button>
            <button type="submit" class="btn">Create</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Edit Agent Modal -->
    <div class="modal-overlay" id="edit-agent-modal" style="display:none">
      <div class="modal">
        <h3><span id="edit-agent-modal-title">Edit Agent</span>: <span id="edit-agent-id"></span></h3>
        <form id="edit-agent-form" onsubmit="submitEditAgent(event)">
          <div class="form-group">
            <label>Display Name</label>
            <input type="text" id="edit-display-name">
          </div>
          <div class="form-group">
            <label>Description</label>
            <input type="text" id="edit-description">
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Owner</label>
              <span id="edit-owner" class="text-muted"></span>
            </div>
            <div class="form-group">
              <label>Default User</label>
              <span id="edit-default-user" class="text-muted"></span>
            </div>
          </div>
          <div class="form-group">
            <label>Can Read From</label>
            {edit_read_html}
            <p class="form-hint">The agent's private scope is automatic and hidden here. Checked items are extra places this agent can retrieve from.</p>
          </div>
          <div class="form-group">
            <label>Can Write To</label>
            {edit_write_html}
            <p class="form-hint">Checked items are extra places this agent can save to.</p>
          </div>
          <div class="form-group">
            <label>Default Recall Scopes</label>
            <label class="checkbox-label"><input type="checkbox" id="edit-recall-all" onchange="toggleRecallPicker('edit')"> <span>Recall from <strong>all</strong> scopes this agent can read</span></label>
            <div id="edit-recall-wrap">
              {edit_recall_html}
              <p class="form-hint"><strong>Checked</strong> = recall from everything the agent can read (the default — same as before this setting existed). <strong>Uncheck</strong> to recall only the scopes ticked below. The agent's own scope is always included, and any other readable scope stays reachable on demand via <code>memory_search(scope=…)</code>.</p>
            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-agent-modal')">Cancel</button>
            <button id="edit-agent-save" type="submit" class="btn">Save Changes</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Rotate Key Modal -->
    <div class="modal-overlay" id="rotate-modal" style="display:none">
      <div class="modal">
        <h3>New API Key</h3>
        <div id="rotate-modal-body"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('rotate-modal'); refreshAgents();">Done</button>
        </div>
      </div>
    </div>
    """,
        "/agents",
        js,
        session=session,
    )


