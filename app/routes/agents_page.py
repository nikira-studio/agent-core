"""Agents dashboard page."""

import json
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends

from app.routes.dashboard_shared import (
    escape_html,
    render_page,
    require_auth,
    local_dt,
    get_icon,
)

router = APIRouter()

# The roles offered on the create and edit forms, as the capability sets they tick.
ROLE_PRESETS = {
    "assistant": ["memory", "coordination", "credentials", "connectors_read", "connectors_execute"],
    "service": ["credentials_resolve"],
    "proposer": ["coordination", "admin_propose"],
}
ROLE_NAMES = {"assistant": "Assistant", "service": "Service agent", "proposer": "Proposer", "custom": "Custom"}


def role_of(capabilities) -> str:
    """The role whose capability set matches exactly, else 'custom'."""
    wanted = set(capabilities)
    return next((role for role, caps in ROLE_PRESETS.items() if set(caps) == wanted), "custom")


@router.get("/agents")
def agents_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import agent_service, auth_service, workspace_service

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
    selectable_users = auth_service.list_users() if is_admin else [
        {"id": session["user_id"], "display_name": "Your personal context"}
    ]

    def build_scope_list(prefix):
        h = f'<div class="scope-selectors" id="{prefix}-scopes">'
        h += '<label class="checkbox-label" data-scope-row="shared"><input type="checkbox" data-scope="shared"> <span>Other users can use this scope</span></label>'
        h += "<h4>Personal User Scopes</h4>"
        for user in selectable_users:
            user_id = user["id"]
            checked = " checked" if prefix == "ca-read" and user_id == session["user_id"] else ""
            required = (
                ' data-required-scope="true" disabled'
                if prefix == "ca-read" and user_id == session["user_id"]
                else ""
            )
            owner_label = " (owner context)" if user_id == session["user_id"] else ""
            user_label = "User" if user_id == session["user_id"] else user.get("display_name") or user_id
            h += (
                f'<label class="checkbox-label" data-scope-row="user:{escape_html(user_id)}">'
                f'<input type="checkbox" data-scope="user:{escape_html(user_id)}"{checked}{required}>'
                f' <span>{escape_html(user_label)} '
                f'<code>user:{escape_html(user_id)}</code>{owner_label}</span></label>'
            )
        if all_workspaces:
            h += "<h4>Workspaces</h4>"
            for p in all_workspaces:
                h += f'<label class="checkbox-label" data-scope-row="workspace:{escape_html(p["id"])}"><input type="checkbox" data-scope="workspace:{escape_html(p["id"])}"> <span>{escape_html(p["name"])} <code>workspace:{escape_html(p["id"])}</code></span></label>'
        if agents:
            h += "<h4>Other Agent Private Scopes</h4>"
            for a in agents:
                h += f'<label class="checkbox-label" data-scope-row="agent:{escape_html(a["id"])}"><input type="checkbox" data-scope="agent:{escape_html(a["id"])}"> <span>{escape_html(a["display_name"])} ({escape_html(a["id"])})</span></label>'
        h += "</div>"
        return h

    ca_read_html = build_scope_list("ca-read")
    ca_write_html = build_scope_list("ca-write")
    ca_recall_html = build_scope_list("ca-recall")
    edit_read_html = build_scope_list("edit-read")
    edit_write_html = build_scope_list("edit-write")
    edit_recall_html = build_scope_list("edit-recall")

    def capability_controls(prefix):
        roles = [
            ("assistant", "Assistant",
             "An AI assistant or coding agent. Remembers things, tracks its work and handoffs, and can use your connected services."),
            ("service", "Service agent",
             "A script or system that only fetches the specific stored credentials an admin has granted it. No memory, no connectors."),
            ("proposer", "Proposer",
             "Suggests admin changes, such as a new agent or connector. Nothing happens until a person approves it on the Approvals page."),
            ("custom", "Custom",
             "Choose each permission yourself."),
        ]
        cards = "".join(
            f'<label class="role-card"><input type="radio" name="{prefix}-role" data-role="{key}"'
            f'{" checked" if key == "assistant" else ""}> '
            f'<span><strong>{name}</strong><small>{description}</small></span></label>'
            for key, name, description in roles
        )
        # (key, name, what it lets the agent do, starts checked on the create form)
        groups = [
            ("Everyday work", [
                ("memory", "Memory", "Search, read and write memory records in the scopes below.", True),
                ("coordination", "Coordination", "Track its own work: activity records, handoffs, the session briefing, workspace sync and approval status.", True),
            ]),
            ("Credentials", [
                ("credentials", "Credential references", "List credentials and ask for the reference an agent uses (AC_SECRET_...). The agent never sees the secret value; Agent Core inserts it when a command or connector runs.", True),
                ("credentials_resolve", "Read credential values", "Fetch a stored value over HTTPS, one exact entry at a time, and only the entries an admin has granted it. For small service agents; leave off for assistants.", False),
            ]),
            ("Connectors", [
                ("connectors_read", "Connector discovery", "See which external services and actions are set up. Always on when Run connector actions is on.", True),
                ("connectors_execute", "Run connector actions", "Have Agent Core perform actions in external services for it, using the credentials in your bindings.", True),
            ]),
            ("Administration", [
                ("admin_propose", "Propose admin changes", "Ask for changes such as new agents, grants or bindings. Nothing happens until a person approves it on the Approvals page.", False),
            ]),
        ]
        controls = ""
        for heading, items in groups:
            controls += f'<h4 class="capability-group">{heading}</h4>'
            for key, name, description, default in items:
                controls += (
                    f'<label class="checkbox-label"><input type="checkbox" data-capability="{key}" '
                    f'id="{prefix}-capability-{key}"{" checked" if default else ""}> '
                    f'<span><strong>{name}</strong><br><small class="form-hint">{description}</small></span></label>'
                )
        return (
            '<div class="form-section"><h4 class="form-section-title">What will this agent do?</h4>'
            f'<div class="role-cards">{cards}</div>'
            f'<div class="role-custom" id="{prefix}-role-custom" hidden>{controls}</div>'
            '<p class="form-hint">Scopes decide which data an agent can reach; this decides which kinds of action it may attempt. '
            'Changes apply to the next request; reconnect an MCP client to refresh its tool list.</p></div>'
        )

    capability_create_html = capability_controls("ca")
    capability_edit_html = capability_controls("edit")

    def access_section(prefix, read_html, write_html, recall_html, create):
        recall_checked = " checked" if create else ""
        recall_hidden = ' style="display:none"' if create else ""
        read_hint = (
            "Your personal scope is inherited and cannot be removed."
            if create else
            "Its private scope and its owner's personal scope are automatic. Tick extra places it may read from."
        )
        return (
            '<div class="form-section"><h4 class="form-section-title">What can it reach?</h4>'
            f'<details class="access-details"><summary>Memory access <span class="text-muted" id="{prefix}-access-summary"></span></summary>'
            '<p class="form-hint">An agent always has its own private scope. Add workspaces to share memory with other agents and people.</p>'
            f'<div class="form-group"><label>Can Read From</label>{read_html}<p class="form-hint">{read_hint}</p></div>'
            f'<div class="form-group"><label>Can Write To</label>{write_html}'
            '<p class="form-hint">Only where the agent may save new memory or credentials. Use workspaces for sharing; writing to a personal scope is an advanced choice.</p></div>'
            '<div class="form-group"><label>Default recall</label>'
            f'<label class="checkbox-label"><input type="checkbox" id="{prefix}-recall-all"{recall_checked}> '
            '<span>Search everything this agent can read</span></label>'
            f'<div id="{prefix}-recall-wrap"{recall_hidden}>{recall_html}'
            '<p class="form-hint">Unticked, searches cover only the scopes ticked here (plus its own). Any other readable scope can still be searched by name.</p></div></div>'
            '</details></div>'
        )

    create_access_html = access_section("ca", ca_read_html, ca_write_html, ca_recall_html, True)
    edit_access_html = access_section("edit", edit_read_html, edit_write_html, edit_recall_html, False)

    def agent_row(a):
        active = a.get("is_active")
        status_badge = f"<span class='badge badge-{'active' if active else 'inactive'}'>{'active' if active else 'inactive'}</span>"
        read_scopes = agent_service.parse_scopes(a.get("read_scopes_json", "[]"))
        write_scopes = agent_service.parse_scopes(a.get("write_scopes_json", "[]"))
        own_scope = f"agent:{a['id']}"
        principal_scope = f"user:{a.get('default_user_id') or a.get('owner_user_id')}"
        read_extra = [s for s in read_scopes if s not in (own_scope, principal_scope)]
        write_extra = [s for s in write_scopes if s != own_scope]
        shared_badge = (
            "<span class='badge badge-info'>Shared</span>"
            if a.get("is_shared") or agent_service.is_agent_shared(a)
            else ""
        )
        access_summary = (
            f"<span class='scope-tag' title='Implicit private scope'>{escape_html(own_scope)}</span>"
            f"<span class='scope-tag' title='Inherited principal scope'>{escape_html(principal_scope)}</span>"
            f"{shared_badge}"
            f"<span class='text-muted'> + {len(read_extra)} read / {len(write_extra)} write grants</span>"
        )
        can_manage = is_admin or a.get("owner_user_id") == session["user_id"]
        if can_manage:
            if active:
                toggle_btn = f"<button type='button' class='btn btn-sm btn-warning' data-agent-deactivate='{escape_html(a['id'])}'>Deactivate</button>"
            else:
                toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' data-agent-reactivate='{escape_html(a['id'])}'>Reactivate</button>"
        else:
            toggle_btn = ""
        role = role_of(agent_service.parse_capabilities(a.get("capabilities_json")))
        role_badge = f"<span class='badge badge-info' title='What this agent may do. Open Edit to change it.'>{ROLE_NAMES[role]}</span>"
        owner_id = a.get("owner_user_id", "")
        default_user_id = a.get("default_user_id", "") or owner_id
        if can_manage:
            action_buttons = (
                f"<button type='button' class='btn btn-sm btn-secondary' data-agent-edit='{escape_html(a['id'])}'>Edit</button>"
                f"<a class='btn btn-sm btn-secondary' href='/integrations?agent_id={quote(a['id'])}'>Connect</a>"
                f"{toggle_btn}"
                f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-agent-purge='{escape_html(a['id'])}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            )
        else:
            action_buttons = (
                f"<button type='button' class='btn btn-sm btn-secondary' data-agent-view='{escape_html(a['id'])}'>View</button>"
            )
        return (
            f"<tr>"
            f"<td><strong>{escape_html(a.get('display_name', ''))}</strong> {role_badge}<br><code>{escape_html(a['id'])}</code>"
            f"<div class='text-muted'>Owner: {escape_html(owner_id)} · Default user: {escape_html(default_user_id)}</div></td>"
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
    let agentModalReadOnly = false;
    async function refreshAgents() { location.reload(); }
    function createAuthorityPreview() {
      const agentId = normalizeAgentId(document.getElementById('ca-id')?.value);
      const ownScope = 'agent:' + (agentId || '<new-agent-id>');
      const read = [ownScope].concat(getSelectedScopes('ca-read-scopes'));
      const write = [ownScope].concat(getSelectedScopes('ca-write-scopes'));
      const recallAll = document.getElementById('ca-recall-all')?.checked;
      const recall = recallAll ? read : [ownScope].concat(getSelectedScopes('ca-recall-scopes'));
      const capabilities = getSelectedCapabilities('ca');
      const preview = document.getElementById('ca-authority-preview');
      if (!preview) return;
      preview.innerHTML = '<strong>Permanent authority after creation</strong><br>' +
        'Read: <code>' + escapeHtml(read.join(', ')) + '</code><br>' +
        'Write: <code>' + escapeHtml(write.join(', ')) + '</code><br>' +
        'Default recall: <code>' + escapeHtml(recall.join(', ')) + '</code><br>' +
        'Capabilities: <code>' + escapeHtml(capabilities.join(', ')) + '</code>';
    }
    function applyLeastPrivilegedPreset() {
      ['ca-read-scopes', 'ca-write-scopes', 'ca-recall-scopes'].forEach(function(containerId) {
        document.querySelectorAll('#' + containerId + ' input').forEach(function(input) {
          input.checked = input.dataset.requiredScope === 'true';
        });
      });
      const recallAll = document.getElementById('ca-recall-all');
      if (recallAll) recallAll.checked = false;
      toggleRecallPicker('ca');
      createAuthorityPreview();
    }
    document.addEventListener('click', function(ev) {
      const edit = ev.target.closest('[data-agent-edit]');
      if (edit) { ev.preventDefault(); editAgent(edit.dataset.agentEdit); return; }
      const view = ev.target.closest('[data-agent-view]');
      if (view) { ev.preventDefault(); viewAgent(view.dataset.agentView); return; }
      const deactivate = ev.target.closest('[data-agent-deactivate]');
      if (deactivate) { ev.preventDefault(); deactivateAgent(deactivate.dataset.agentDeactivate); return; }
      const reactivate = ev.target.closest('[data-agent-reactivate]');
      if (reactivate) { ev.preventDefault(); reactivateAgent(reactivate.dataset.agentReactivate); return; }
      const purge = ev.target.closest('[data-agent-purge]');
      if (purge) { ev.preventDefault(); purgeAgent(purge.dataset.agentPurge); }
    });
    document.addEventListener('input', function(ev) {
      if (ev.target.closest('#create-agent-form')) createAuthorityPreview();
    });
    document.addEventListener('change', function(ev) {
      if (ev.target.closest('#create-agent-form')) {
        toggleRecallPicker('ca');
        createAuthorityPreview();
      }
    });

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
          '<div class="api-key-display"><code>' + escapeHtml(j.data.api_key) + '</code></div>' +
          '<button class="copy-btn" data-copy-value="' + escapeHtml(j.data.api_key) + '">Copy</button>';
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
          if (input.id === 'edit-display-name' || input.id === 'edit-description' || input.dataset.capability || input.dataset.role) {
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
      setSelectedCapabilities('edit', a.capabilities || []);
      syncRole('edit');
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
      updateAccessSummary('edit');
      // Hide and disable the agent's own scope row: self-access is implicit.
      ['edit-read-scopes', 'edit-write-scopes', 'edit-recall-scopes'].forEach(containerId => {
        const isRecall = containerId === 'edit-recall-scopes';
        document.querySelectorAll('#' + containerId + ' input').forEach(input => {
          const isOwnScope = input.dataset.scope === 'agent:' + a.id;
          const isPrincipalRead = containerId === 'edit-read-scopes' &&
            input.dataset.scope === 'user:' + (a.default_user_id || a.owner_user_id);
          if (isPrincipalRead) input.checked = true;
          input.disabled = Boolean(readOnly) || isOwnScope || isPrincipalRead || input.dataset.requiredScope === 'true';
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
      function getSelectedCapabilities(prefix) {
        return Array.from(document.querySelectorAll('#' + prefix + '-capability-controls [data-capability]:checked')).map(i => i.dataset.capability);
      }
      function setSelectedCapabilities(prefix, capabilities) {
        document.querySelectorAll('#' + prefix + '-capability-controls [data-capability]').forEach(i => { i.checked = capabilities.includes(i.dataset.capability); });
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
      const ROLE_PRESETS = __ROLE_PRESETS__;
      function applyRole(prefix, role) {
        if (ROLE_PRESETS[role]) setSelectedCapabilities(prefix, ROLE_PRESETS[role]);
        const custom = document.getElementById(prefix + '-role-custom');
        if (custom) custom.hidden = role !== 'custom';
      }
      function syncRole(prefix) {
        const caps = getSelectedCapabilities(prefix).slice().sort().join(',');
        const match = Object.keys(ROLE_PRESETS).find(r => ROLE_PRESETS[r].slice().sort().join(',') === caps);
        const role = match || 'custom';
        const radio = document.querySelector('input[name="' + prefix + '-role"][data-role="' + role + '"]');
        if (radio) radio.checked = true;
        applyRole(prefix, role);
      }
      function updateAccessSummary(prefix) {
        const el = document.getElementById(prefix + '-access-summary');
        if (!el) return;
        const own = 'agent:' + (prefix === 'ca' ? normalizeAgentId(document.getElementById('ca-id')?.value) : document.getElementById('edit-agent-id').textContent);
        const principal = prefix === 'edit' ? 'user:' + document.getElementById('edit-default-user').textContent : '';
        const count = id => Array.from(document.querySelectorAll('#' + id + ' input:checked'))
          .filter(i => !i.dataset.requiredScope && i.dataset.scope !== own && i.dataset.scope !== principal).length;
        const r = count(prefix + '-read-scopes'), w = count(prefix + '-write-scopes');
        el.textContent = '(' + (r || w ? r + ' extra to read, ' + w + ' to write' : 'private scope only') + ')';
      }
      document.addEventListener('change', function(ev) {
        const t = ev.target;
        if (t.id && t.id.endsWith('-recall-all')) toggleRecallPicker(t.id.replace('-recall-all', ''));
        if (t.dataset && t.dataset.role) applyRole(t.name.replace('-role', ''), t.dataset.role);
        if (t.dataset && t.dataset.capability === 'connectors_execute' && t.checked) {
          const read = document.getElementById(t.id.replace('execute', 'read'));
          if (read) read.checked = true;
        }
        const form = t.closest && t.closest('form');
        if (form && (form.id === 'create-agent-form' || form.id === 'edit-agent-form')) {
          updateAccessSummary(form.id === 'create-agent-form' ? 'ca' : 'edit');
        }
      });
      function normalizeAgentId(value) {
        return (value || '').trim().toLowerCase();
      }

      async function submitEditAgent(e) {
      e.preventDefault();
      if (agentModalReadOnly) return;
      const id = document.getElementById('edit-agent-id').textContent;
        const ownScope = 'agent:' + id;
        const body = {
          display_name: document.getElementById('edit-display-name').value,
          description: document.getElementById('edit-description').value,
          read_scopes: getSelectedScopes('edit-read-scopes'),
          write_scopes: getSelectedScopes('edit-write-scopes'),
          capabilities: getSelectedCapabilities('edit'),
        };
        body.read_scopes.push(ownScope);
        body.write_scopes.push(ownScope);
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
          capabilities: getSelectedCapabilities('ca'),
        };
        // Ensure private scope is added if not present (it's implicit in backend but good to show)
        const privateScope = 'agent:' + agentId;
        if (!body.read_scopes.includes(privateScope)) body.read_scopes.push(privateScope);
        if (!body.write_scopes.includes(privateScope)) body.write_scopes.push(privateScope);
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
      document.addEventListener('DOMContentLoaded', function() { createAuthorityPreview(); updateAccessSummary('ca'); });
    </script>"""
    js = js.replace("__IS_ADMIN__", str(is_admin).lower()).replace("__ROLE_PRESETS__", json.dumps(ROLE_PRESETS))

    return render_page(
        "Agents",
        f"""
    <div class="page-header"><h1>Agents</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-agent-modal')">+ Create Agent</button>
    </div></div>
    <div class="card">
      <h3>Agent Access</h3>
      <p class="text-muted access-summary">An agent always has its private scope and read access to its active principal's user scope. To share memory with other agents and people, add a workspace.</p>
    </div>

    <div class="card">
      <h3>All Agents</h3>
<table><thead><tr><th>Agent</th><th>Status</th><th>Access</th><th>Created</th><th class="actions-cell">Actions</th></tr></thead>
        <tbody>{rows or "<tr><td colspan=5 class=empty>No agents yet.</td></tr>"}</tbody></table>
    </div>

    <!-- Create Agent Modal -->
    <div class="modal-overlay" id="create-agent-modal" style="display:none">
      <div class="modal modal-wide">
        <h3>Create Agent</h3>
        <form id="create-agent-form" onsubmit="createAgent(event)">
          <div class="form-section">
            <div class="form-row">
              <div class="form-group">
                <label>Agent ID *</label>
                <input type="text" id="ca-id" pattern="[a-z0-9_\\-]+" placeholder="e.g. coding-agent" required>
                <p class="form-hint">Lowercase letters, numbers, hyphens, underscores.</p>
              </div>
              <div class="form-group">
                <label>Display name *</label>
                <input type="text" id="ca-display-name" required>
              </div>
            </div>
            <div class="form-group">
              <label>Description</label>
              <input type="text" id="ca-description" placeholder="What is this agent for?">
            </div>
          </div>
          <div id="ca-capability-controls">{capability_create_html}</div>
          {create_access_html}
          <div class="form-group">
            <button type="button" class="btn btn-sm btn-secondary" onclick="applyLeastPrivilegedPreset()">Reset to private access only</button>
            <span class="form-hint">Clears every extra scope; the agent keeps its private scope and its owner's read access.</span>
          </div>
          <div id="ca-authority-preview" class="form-hint" style="white-space:normal"></div>
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
      <div class="modal modal-wide">
        <h3><span id="edit-agent-modal-title">Edit Agent</span>: <span id="edit-agent-id"></span></h3>
        <form id="edit-agent-form" onsubmit="submitEditAgent(event)">
          <div class="form-section">
            <div class="form-row">
              <div class="form-group">
                <label>Display name</label>
                <input type="text" id="edit-display-name">
              </div>
              <div class="form-group">
                <label>Description</label>
                <input type="text" id="edit-description">
              </div>
            </div>
            <p class="form-hint">Owner: <span id="edit-owner"></span> · Default user: <span id="edit-default-user"></span></p>
          </div>
          <div id="edit-capability-controls">{capability_edit_html}</div>
          {edit_access_html}
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
