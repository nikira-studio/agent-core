from fastapi import APIRouter, Depends, Request
from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer
from app.services import vault_service
from app.services import workspace_service
from app.services import connector_service
from app.services.agent_service import list_agents
from app.routes.dashboard import render_page, escape_html, require_auth

router = APIRouter()


@router.get("/connectors")
async def connectors_page(request: Request, session: dict = Depends(require_auth)):
    ctx = build_user_context(session)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    connector_types = connector_service.list_connector_types()

    all_bindings = connector_service.list_bindings()
    visible_bindings = [b for b in all_bindings if enforcer.can_read(b["scope"])]

    workspaces = (
        workspace_service.list_workspaces()
        if ctx.is_admin
        else workspace_service.list_workspaces(owner_user_id=ctx.user_id)
    )

    agents = list_agents() if ctx.is_admin else list_agents(owner_user_id=ctx.user_id)

    vault_entries = [
        e
        for e in (vault_service.list_vault_entries(limit=500) or [])
        if enforcer.can_read(e.get("scope", ""))
    ]

    user_scope = f"user:{session['user_id']}"
    workspace_scope_opts = "".join(
        f'<option value="workspace:{p["id"]}">workspace:{p["id"]}</option>'
        for p in workspaces
    )
    agent_scope_opts = "".join(
        f'<option value="agent:{a["id"]}">agent:{a["id"]}</option>'
        for a in agents
        if a.get("is_active")
    )
    scope_options = (
        f'<option value="{user_scope}">{user_scope}</option>\n'
        f"{workspace_scope_opts}\n"
        f"{agent_scope_opts}"
    )

    connector_type_opts = "".join(
        f'<option value="{ct["id"]}">{escape_html(ct["display_name"])}</option>'
        for ct in connector_types
    )

    vault_opts = "".join(
        f'<option value="{e["id"]}">{escape_html(e.get("name", e["id"]))} ({escape_html(e.get("scope", ""))} / {escape_html(e.get("reference_name", ""))})</option>'
        for e in vault_entries
    )

    bindings_rows = ""
    for b in visible_bindings:
        ct = next(
            (c for c in connector_types if c["id"] == b["connector_type_id"]), None
        )
        if b.get("enabled") and not b.get("last_error"):
            status_cls = "status-ok"
            status_text = "Enabled" if b.get("enabled") else "Disabled"
        else:
            status_cls = "status-error"
            status_text = (
                f"Error"
                if b.get("last_error")
                else ("Disabled" if not b.get("enabled") else "OK")
            )
        if b.get("last_error"):
            status_text = f"Error: {str(b['last_error'])[:40]}"
        elif b.get("last_tested_at"):
            status_text = f"OK ({b['last_tested_at'][:10]})"
        bindings_rows += f"""
        <tr data-binding-id="{b["id"]}">
          <td>{escape_html(b.get("name", ""))}</td>
          <td>{escape_html(ct.get("display_name", "") if ct else b.get("connector_type_id", ""))}</td>
          <td><code>{escape_html(b.get("scope", ""))}</code></td>
          <td class="{status_cls}">{escape_html(status_text)}</td>
          <td>
            <button type='button' class='btn btn-sm btn-secondary' onclick='editBinding("{b["id"]}")'>Edit</button>
            <button type='button' class='btn btn-sm btn-secondary' onclick='testBinding("{b["id"]}")'>Test</button>
            <button type='button' class='btn btn-sm btn-danger' onclick='deleteBinding("{b["id"]}")'>Delete</button>
          </td>
        </tr>"""

    if visible_bindings:
        bindings_html = f"""
        <table><thead><tr><th>Name</th><th>Type</th><th>Scope</th><th>Status</th><th class='actions-cell'>Actions</th></tr></thead>
        <tbody>{bindings_rows}</tbody></table>"""
    else:
        bindings_html = (
            "<div class='empty'>No connector bindings yet. Create one below.</div>"
        )

    ct_cards = ""
    for ct in connector_types:
        actions = ", ".join(
            f"<code>{a}</code>" for a in ct.get("supported_actions", [])
        )
        ct_cards += f"""
        <div class='connector-type-card'>
          <div class='connector-type-name'>{escape_html(ct["display_name"])}</div>
          <div class='connector-type-desc'>{escape_html(ct.get("description", "") or "No description")}</div>
          <div class='connector-type-meta'>
            Auth: <code>{ct.get("auth_type", "")}</code> |
            Actions: {actions}
          </div>
        </div>"""

    body = f"""
    <div class="page-header"><h1>Connectors</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-binding-modal')">+ New Binding</button>
    </div></div>

    <div class="card">
      <div class="section-header"><h3>Connector Types</h3></div>
      <div class="connector-types-grid">{ct_cards or "<div class='empty'>No connector types registered.</div>"}</div>
    </div>

    <div class="card">
      <div class="section-header"><h3>Bindings</h3></div>
      <div id="bindings-list">{bindings_html}</div>
    </div>

    <!-- Create Binding Modal -->
    <div class="modal-overlay" id="create-binding-modal" style="display:none">
      <div class="modal">
        <h3>New Binding</h3>
        <form id="create-binding-form" onsubmit="createBinding(event)">
          <div class="form-group">
            <label>Connector Type *</label>
            <select id="binding-connector-type" required>
              <option value="">-- Select --</option>
              {connector_type_opts}
            </select>
          </div>
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="binding-name" placeholder="e.g. My GitHub" required>
          </div>
          <div class="form-group">
            <label>Scope *</label>
            <select id="binding-scope" required>
              <option value="">Select scope...</option>
              {scope_options}
            </select>
          </div>
          <div class="form-group">
            <label>Credential</label>
            <select id="binding-credential">
              <option value="">-- Select stored credential --</option>
              {vault_opts}
            </select>
          </div>
          <div class="form-group">
            <label>Config (JSON, optional)</label>
            <textarea id="binding-config" rows="2" placeholder='{{"repo": "owner/name"}}'></textarea>
          </div>
          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" id="binding-enabled" checked> Enabled
            </label>
          </div>
          <button type="submit" class="btn btn-primary">Create Binding</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('create-binding-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Edit Binding Modal -->
    <div class="modal-overlay" id="edit-binding-modal" style="display:none">
      <div class="modal">
        <h3>Edit Binding</h3>
        <form id="edit-binding-form" onsubmit="submitEditBinding(event)">
          <input type="hidden" id="edit-binding-id">
          <div class="form-group">
            <label>Name</label>
            <input type="text" id="edit-binding-name">
          </div>
          <div class="form-group">
            <label>Scope</label>
            <select id="edit-binding-scope">
              {scope_options}
            </select>
          </div>
          <div class="form-group">
            <label>Credential</label>
            <select id="edit-binding-credential">
              <option value="">-- Select --</option>
              {vault_opts}
            </select>
          </div>
          <div class="form-group">
            <label>Config (JSON)</label>
            <textarea id="edit-binding-config" rows="2"></textarea>
          </div>
          <div class="form-group">
            <label class="checkbox-label">
              <input type="checkbox" id="edit-binding-enabled"> Enabled
            </label>
          </div>
          <button type="submit" class="btn btn-primary">Save Changes</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('edit-binding-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Test Result Modal -->
    <div class="modal-overlay" id="test-result-modal" style="display:none">
      <div class="modal">
        <h3>Connection Test Result</h3>
        <div id="test-result-content"></div>
        <button type="button" class="btn" onclick="closeModal('test-result-modal')">Close</button>
      </div>
    </div>

    <!-- Binding Executions Modal -->
    <div class="modal-overlay" id="executions-modal" style="display:none">
      <div class="modal" style="max-width:700px">
        <h3>Execution History</h3>
        <div id="executions-content"></div>
        <button type="button" class="btn" onclick="closeModal('executions-modal')">Close</button>
      </div>
    </div>
    """

    js = """
<script>
async function createBinding(e) {
  e.preventDefault();
  const body = {
    connector_type_id: document.getElementById('binding-connector-type').value,
    name: document.getElementById('binding-name').value,
    scope: document.getElementById('binding-scope').value,
    credential_id: document.getElementById('binding-credential').value || null,
    config_json: document.getElementById('binding-config').value || null,
    enabled: document.getElementById('binding-enabled').checked,
  };
  const j = await apiFetch('/api/connector-bindings', { method: 'POST', body: JSON.stringify(body) });
  if (j.ok) {
    showToast('Binding created', 'success');
    closeModal('create-binding-modal');
    document.getElementById('create-binding-form').reset();
    location.reload();
  } else {
    showToast(j.error?.message || 'Failed to create binding', 'danger');
  }
}

async function editBinding(id) {
  const j = await apiFetch('/api/connector-bindings/' + id);
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const b = j.data.binding;
  document.getElementById('edit-binding-id').value = id;
  document.getElementById('edit-binding-name').value = b.name || '';
  document.getElementById('edit-binding-scope').value = b.scope || '';
  document.getElementById('edit-binding-credential').value = b.credential_id || '';
  document.getElementById('edit-binding-config').value = b.config_json || '';
  document.getElementById('edit-binding-enabled').checked = !!b.enabled;
  openModal('edit-binding-modal');
}

async function submitEditBinding(e) {
  e.preventDefault();
  const id = document.getElementById('edit-binding-id').value;
  const body = {
    name: document.getElementById('edit-binding-name').value,
    scope: document.getElementById('edit-binding-scope').value,
    credential_id: document.getElementById('edit-binding-credential').value || null,
    config_json: document.getElementById('edit-binding-config').value || null,
    enabled: document.getElementById('edit-binding-enabled').checked,
  };
  const j = await apiFetch('/api/connector-bindings/' + id, { method: 'PUT', body: JSON.stringify(body) });
  if (j.ok) { showToast('Updated', 'success'); closeModal('edit-binding-modal'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function deleteBinding(id) {
  if (!confirm('Delete this binding? This cannot be undone.')) return;
  const j = await apiFetch('/api/connector-bindings/' + id, { method: 'DELETE' });
  if (j.ok) { showToast('Deleted', 'success'); location.reload(); }
  else { showToast(j.error?.message || 'Failed', 'danger'); }
}

async function testBinding(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/test', { method: 'POST' });
  if (j.ok) {
    const r = j.data.result;
    const content = document.getElementById('test-result-content');
    if (r.success) {
      content.innerHTML = '<div class="alert alert-success">Connection successful!</div>';
    } else {
      content.innerHTML = '<div class="alert alert-danger">Connection failed: ' + escapeHtml(r.error || 'Unknown error') + '</div>';
    }
    openModal('test-result-modal');
  } else {
    showToast(j.error?.message || 'Failed to test binding', 'danger');
  }
}

async function viewExecutions(id) {
  const j = await apiFetch('/api/connector-bindings/' + id + '/executions');
  if (!j.ok) { showToast(j.error?.message || 'Error', 'danger'); return; }
  const execs = j.data.executions || [];
  const rows = execs.map(function(e) {
    return '<tr><td>' + escapeHtml(e.action || '') + '</td><td>' + escapeHtml(e.result_status || '') + '</td><td>' + escapeHtml(e.executed_at || '') + '</td><td>' + escapeHtml(e.error_message || '-') + '</td></tr>';
  }).join('');
  document.getElementById('executions-content').innerHTML = execs.length
    ? '<table><thead><tr><th>Action</th><th>Status</th><th>When</th><th>Error</th></tr></thead><tbody>' + rows + '</tbody></table>'
    : '<em>No executions yet.</em>';
  openModal('executions-modal');
}

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, function(c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}
</script>"""

    return render_page("Connectors", body, "/connectors", js, session=session)
