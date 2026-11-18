"""Agents dashboard page."""

import json
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse

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
ROLE_NAMES = {"assistant": "Assistant", "service": "Service", "proposer": "Proposer", "custom": "Custom"}


def role_of(capabilities) -> str:
    """The role whose capability set matches exactly, else 'custom'."""
    wanted = set(capabilities)
    return next((role for role, caps in ROLE_PRESETS.items() if set(caps) == wanted), "custom")


@router.get("/agents")
def agents_page(request: Request, session: dict = Depends(require_auth)):
    if request.query_params.get("show") == "services":
        return RedirectResponse("/services", status_code=302)  # the old address of the Services list
    return _agents_view(request, session, show_services=False)


@router.get("/services")
def services_page(request: Request, session: dict = Depends(require_auth)):
    """The programs and containers that only fetch secrets: a tab under Credentials."""
    return _agents_view(request, session, show_services=True)


def _agents_view(request: Request, session: dict, show_services: bool):
    from app.services import agent_access_service, agent_service, auth_service, workspace_service

    is_admin = session.get("role") == "admin"
    # /agents lists the agents that do work; /services lists the programs that only fetch secrets.

    def entries(kind, active_only=False):
        """The entries of one kind this viewer may see."""
        is_active = True if active_only else None
        if is_admin:
            return agent_service.list_agents(is_active=is_active, kind=kind)
        return agent_service.list_visible_agents(session["user_id"], is_active=is_active, kind=kind)

    agents = entries("agent")
    services = entries("service")
    # The switch counts the entries that are switched on: an entry that is off is not on anyone's list.
    agent_count = len(entries("agent", active_only=True))
    service_count = len(entries("service", active_only=True))
    listed = services if show_services else agents

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
        h += '<label class="checkbox-label" data-scope-row="shared"><input type="checkbox" data-scope="shared"> <span>Shared memory: visible across all users and workspaces</span></label>'
        h += "<h3 class='sub-heading'>Personal</h3>"
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
            h += "<h3 class='sub-heading'>Workspaces</h3>"
            for p in all_workspaces:
                h += f'<label class="checkbox-label" data-scope-row="workspace:{escape_html(p["id"])}"><input type="checkbox" data-scope="workspace:{escape_html(p["id"])}"> <span>{escape_html(p["name"])} <code>workspace:{escape_html(p["id"])}</code></span></label>'
        if agents:
            h += "<h3 class='sub-heading'>Private to other agents</h3>"
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
             "An AI assistant or coding agent. Remembers things, tracks its work and handoffs, and can use your connected capabilities."),
            ("service", "Service",
             "A program or container that only fetches the specific stored secrets an admin has granted it. No memory, no workspaces."),
            ("proposer", "Proposer",
             "Suggests admin changes, such as a new agent or service. Nothing happens until a person approves it on the Approvals page."),
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
                ("memory", "Use memory", "Search, read and write memories in the workspaces below.", True),
                ("coordination", "Coordinate tasks", "Track its own tasks, handoffs, the session briefing, workspace sync and approval status.", True),
            ]),
            ("Credentials", [
                ("credentials", "Reference credentials", "List credentials and ask for the reference an agent uses (AC_SECRET_...). The agent never sees the secret value; Agent Core inserts it when a command or service runs.", True),
                ("credentials_resolve", "Fetch secrets", "Fetch a stored value over HTTPS, one exact entry at a time, and only the entries an admin has granted it. Only for services. An agent does not need it.", False),
            ]),
            ("Capabilities", [
                ("connectors_read", "See capabilities", "See which external capabilities and actions are set up. Always on when Run capabilities is on.", True),
                ("connectors_execute", "Run capabilities", "Have Agent Core perform actions in external capabilities for it, using the credentials in your connections.", True),
            ]),
            ("Administration", [
                ("admin_propose", "Propose admin changes", "Ask for changes such as new agents, grants or connections. Nothing happens until a person approves it on the Approvals page.", False),
            ]),
        ]
        controls = ""
        for heading, items in groups:
            controls += f'<h3 class="sub-heading capability-group">{heading}</h3>'
            for key, name, description, default in items:
                controls += (
                    f'<label class="checkbox-label"><input type="checkbox" data-capability="{key}" '
                    f'id="{prefix}-capability-{key}"{" checked" if default else ""}> '
                    f'<span><strong>{name}</strong><br><small class="form-hint">{description}</small></span></label>'
                )
        return (
            '<div class="form-section"><h3 class="sub-heading form-section-title">What will this agent do?</h3>'
            f'<div class="role-cards">{cards}</div>'
            f'<div class="role-custom" id="{prefix}-role-custom" hidden>'
            '<h3 class="sub-heading form-section-title">Permissions</h3>'
            f'{controls}</div>'
            f'<label class="checkbox-label" style="margin-top:12px"><input type="checkbox" id="{prefix}-inherit-user-bindings"> '
            '<span><strong>Run its person\'s own capabilities</strong><br><small class="form-hint">Lets this agent also run the capabilities '
            'bound in its person\'s personal scope, not only those in workspaces it is given. Off by default; turn it on for an agent '
            'that acts as that person.</small></span></label>'
            '<p class="form-hint">Workspace access decides which data an agent can reach; this decides which kinds of action it may attempt. '
            'Changes apply to the next request; reconnect an MCP client to refresh its tool list.</p></div>'
        )

    capability_create_html = capability_controls("ca")
    capability_edit_html = capability_controls("edit")

    def simple_workspace_choice():
        """One tick per workspace, read and write together: the way in for the common case. The separate read/write
        ticks stay under Advanced access. With exactly one workspace it is ticked for you."""
        if not all_workspaces:
            return (
                '<div class="form-section"><h3 class="sub-heading form-section-title">Which workspace will it work in?</h3>'
                '<p class="form-hint">You have no workspace yet. A workspace is where agents share memory and tasks: '
                '<a href="/workspaces">create one</a> first, then agents you add can work in it. '
                'Without one this agent keeps only its own private memory.</p></div>'
            )
        only = len(all_workspaces) == 1
        boxes = "".join(
            f'<label class="checkbox-label"><input type="checkbox" data-simple-workspace="workspace:{escape_html(p["id"])}"'
            f'{" checked" if only else ""}> <span>{escape_html(p["name"])} <small class="form-hint">read and write</small></span></label>'
            for p in all_workspaces
        )
        return (
            '<div class="form-section"><h3 class="sub-heading form-section-title">Which workspace will it work in?</h3>'
            f'{boxes}'
            '<p class="form-hint">Agents in the same workspace share its memory and tasks. Leave all unticked for an agent '
            'that keeps only its own private memory.</p></div>'
        )

    def access_section(prefix, read_html, write_html, recall_html, create):
        recall_checked = " checked" if create else ""
        recall_hidden = ' style="display:none"' if create else ""
        read_hint = (
            "Your personal scope is inherited and cannot be removed."
            if create else
            "Its private scope and its owner's personal scope are automatic. Tick extra places it may read from."
        )
        simple = simple_workspace_choice() if create else ""
        return (
            simple
            + '<div class="form-section"><h3 class="sub-heading form-section-title">What else can it reach?</h3>'
            f'<details class="access-details"><summary>Advanced access <span class="text-muted" id="{prefix}-access-summary"></span></summary>'
            '<p class="form-hint">An agent always has its own private memory. Add workspaces to share memory with other agents and people.</p>'
            f'<div class="form-group"><label>Can Read From</label>{read_html}<p class="form-hint">{read_hint}</p></div>'
            f'<div class="form-group"><label>Can Write To</label>{write_html}'
            '<p class="form-hint">Only where the agent may save new memory or credentials. Use workspaces for sharing; writing to personal memory is an advanced choice.</p></div>'
            '<div class="form-group"><label>Default recall</label>'
            f'<label class="checkbox-label"><input type="checkbox" id="{prefix}-recall-all"{recall_checked}> '
            '<span>Search everything this agent can read</span></label>'
            f'<div id="{prefix}-recall-wrap"{recall_hidden}>{recall_html}'
            '<p class="form-hint">Unticked, searches cover only the workspaces ticked here (plus its own private memory). Any other workspace it can read can still be searched by name.</p></div></div>'
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
        personal_badge = (
            "<span class='badge badge-info' title='May run the capabilities set up in its owner&#39;s personal space'>runs personal capabilities</span>"
            if a.get("inherit_user_bindings")
            else ""
        )
        access_summary = (
            f"<span class='scope-tag' title='Implicit private scope'>{escape_html(own_scope)}</span>"
            f"<span class='scope-tag' title='Inherited from its owner'>{escape_html(principal_scope)}</span>"
            f"{shared_badge}"
            f"{personal_badge}"
            f"<span class='text-muted'> + {len(read_extra)} read / {len(write_extra)} write grants</span>"
        )
        can_manage = is_admin or a.get("owner_user_id") == session["user_id"]
        no_effect_html = "".join(
            f"<div class='agent-no-effect' style='color:var(--warning-text,#8a5a0a);font-size:0.8rem;margin-top:4px'>{escape_html(n['message'])}</div>"
            for n in agent_access_service.no_effect(a)
        )
        if can_manage:
            if active:
                toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' data-agent-deactivate='{escape_html(a['id'])}'>Deactivate</button>"
            else:
                toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' data-agent-reactivate='{escape_html(a['id'])}'>Reactivate</button>"
        else:
            toggle_btn = ""
        # A service is one of the kinds now, so its row says so; an agent's row still shows the role
        # its permissions match.
        kind = a.get("kind") or "agent"
        role = "service" if kind == "service" else role_of(agent_service.parse_capabilities(a.get("capabilities_json")))
        role_badge = f"<span class='badge badge-info' title='What this {kind} may do. Open Edit to change it.'>{ROLE_NAMES[role]}</span>"
        key_count_text = ""
        if is_admin or a.get("owner_user_id") == session["user_id"]:
            key_total = len(agent_service.list_agent_keys(a["id"]))
            key_count_label = "no keys" if key_total == 0 else ("1 key" if key_total == 1 else f"{key_total} keys")
            # The count is a button: the keys are one click away, without hunting for More.
            key_count_text = (
                " · <button type='button' class='link-button' "
                f"data-agent-keys='{escape_html(a['id'])}' "
                "title='See, add, rename and revoke this agent&#39;s keys'>"
                f"{escape_html(key_count_label)}</button>"
            )
        owner_id = a.get("owner_user_id", "")
        if can_manage:
            # What this entry can reach is one dialog: an agent's workspaces, capabilities and credentials; a
            # service's secrets. Only an admin may open it, so only an admin is offered it.
            access_button = ""
            if is_admin:
                if kind == "service":
                    access_hook = "service"
                    access_title = "What this service can fetch"
                else:
                    access_hook = "agent"
                    access_title = "What this agent can do and reach"
                access_button = (
                    f"<button type='button' class='btn btn-sm btn-secondary' data-{access_hook}-access='{escape_html(a['id'])}'"
                    f" title='{access_title}'>Access</button>"
                )
            # The Connect page sets up an agent that does work; a service is set up with a key and a secret.
            connect_link = (
                ""
                if kind == "service"
                else f"<a class='btn btn-sm btn-secondary' href='/integrations?agent_id={quote(a['id'])}'>Connect</a>"
            )
            # Edit and Connect are what people come for; the rest sit under More, so Delete is never
            # a neighbour of Deactivate.
            action_buttons = (
                f"<button type='button' class='btn btn-sm btn-secondary' data-agent-edit='{escape_html(a['id'])}'>Edit</button>"
                f"{connect_link}"
                "<details class='row-more'><summary class='btn btn-sm btn-secondary' title='Access, keys, deactivate, delete'>More</summary>"
                "<div class='row-more-list'>"
                f"{access_button}"
                f"<button type='button' class='btn btn-sm btn-secondary' data-agent-keys='{escape_html(a['id'])}' title='See, add and revoke this agent&#39;s keys'>Keys</button>"
                f"{toggle_btn}"
                f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-agent-purge='{escape_html(a['id'])}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')} Delete</button>"
                "</div></details>"
            )
        else:
            action_buttons = (
                f"<button type='button' class='btn btn-sm btn-secondary' data-agent-view='{escape_html(a['id'])}'>View</button>"
            )
        return (
            f"<tr>"
            f"<td><strong>{escape_html(a.get('display_name', ''))}</strong> {role_badge}<br><code>{escape_html(a['id'])}</code>"
            f"<div class='text-muted'>Owner: {escape_html(owner_id)}{key_count_text}</div></td>"
            f"<td data-label='Status'>{status_badge}</td>"
            f"<td data-label='Access'>{access_summary}{no_effect_html}</td>"
            f"<td class='hide-phone'>{local_dt(a.get('created_at'), style='date')}</td>"
            f"<td><div class='actions-cell'>"
            f"{action_buttons}"
            f"</div></td></tr>"
        )

    rows = "".join(agent_row(a) for a in listed)

    # The switch above the list: agents on one side, services on the other, each with its own count.
    agents_link_class = "agent-switch-link active" if not show_services else "agent-switch-link"
    services_link_class = "agent-switch-link active" if show_services else "agent-switch-link"
    agents_current = "" if show_services else ' aria-current="page"'
    services_current = ' aria-current="page"' if show_services else ""
    switch_html = (
        '<nav class="agent-switch" aria-label="Agents or services">'
        f'<a href="/agents" class="{agents_link_class}"{agents_current}>Agents ({agent_count})</a>'
        f'<a href="/services" class="{services_link_class}"{services_current}>Services ({service_count})</a>'
        "</nav>"
    )
    services_note = (
        '<p class="text-muted">Programs and containers that fetch secrets. Give one a secret from '
        "Credentials, in the Used by column.</p>"
        if show_services
        else ""
    )
    list_title = "All Services" if show_services else "All Agents"
    list_column = "Service" if show_services else "Agent"
    empty_text = (
        "No services yet. Create one with the Service role."
        if show_services
        else "No agents yet. Create one for each tool or script that should have its own key and history."
    )
    empty_row = f'<tr><td colspan=5 class=empty>{escape_html(empty_text)}</td></tr>'
    # The rows the page shows, so the script knows what each one is without asking the server again.
    # Only the id and the kind: a name is written into the page as text, never into a script.
    row_data = json.dumps(
        [
            {
                "id": a["id"],
                "kind": a.get("kind") or "agent",
            }
            for a in listed
        ]
    ).replace("<", "\\u003c")

    # The workspaces the page's own forms offer, so the Access dialog can offer the ones that are not listed yet.
    workspace_data = json.dumps(
        [{"id": p["id"], "name": p.get("name") or p["id"]} for p in all_workspaces]
    ).replace("<", "\\u003c")

    js = """
    <script>
    const IS_ADMIN = __IS_ADMIN__;
    const AGENT_ROWS = __AGENT_ROWS__;
    // The workspaces this page already knows, so the Access dialog can offer the ones that are not listed yet.
    const ACCESS_WORKSPACES = __ACCESS_WORKSPACES__;
    const CAPABILITY_NAMES = {memory: 'Use memory', coordination: 'Coordinate tasks', credentials: 'Reference credentials', credentials_resolve: 'Fetch secrets', connectors_read: 'See capabilities', connectors_execute: 'Run capabilities', admin_propose: 'Propose admin changes'};
    let agentModalReadOnly = false;
    let editEntryIsService = false;
    async function refreshAgents() { location.reload(); }
    // The Services list opens the create dialog with the Service role already chosen.
    function openCreateService() {
      const radio = document.querySelector('input[name="ca-role"][data-role="service"]');
      if (radio) { radio.checked = true; applyRole('ca', 'service'); }
      openModal('create-agent-modal');
    }
    function rowKind(id) {
      const row = AGENT_ROWS.find(function(r) { return r.id === id; });
      return row ? row.kind : 'agent';
    }
    function selectedRole(prefix) {
      const radio = document.querySelector('input[name="' + prefix + '-role"]:checked');
      return radio ? radio.dataset.role : '';
    }
    // A service only fetches secrets, so the create form drops the memory, workspace and personal choices.
    function setServiceChoice(isService) {
      ['ca-access', 'ca-access-reset'].forEach(function(id) {
        const el = document.getElementById(id);
        if (el) el.hidden = isService;
      });
      const note = document.getElementById('ca-service-note');
      if (note) note.hidden = !isService;
      const preview = document.getElementById('ca-authority-preview');
      if (!preview) return;
      if (isService) {
        preview.innerHTML = '<strong>What this service will be able to do</strong>' +
          '<ul style="margin:6px 0 0 18px">' +
          '<li>Fetch the secrets it is given, one entry at a time</li>' +
          '</ul>';
      } else {
        createAuthorityPreview();
      }
    }
    // The edit form of a service shows only its name and description, and the way to change it.
    function setEditServiceMode(isService) {
      ['edit-capability-controls', 'edit-access', 'edit-owner-line'].forEach(function(id) {
        const el = document.getElementById(id);
        if (el) el.hidden = isService;
      });
      const note = document.getElementById('edit-service-note');
      if (note) note.hidden = !isService;
    }
    function showEditError(message) {
      const errorBox = document.getElementById('edit-agent-error');
      if (errorBox) { errorBox.textContent = message; errorBox.style.display = 'block'; }
      showToast(message, 'danger');
    }
    function createAuthorityPreview() {
      if (selectedRole('ca') === 'service') return;  // a service has one permission and no scopes
      const agentId = normalizeAgentId(document.getElementById('ca-id')?.value);
      const ownScope = 'agent:' + (agentId || '<new-agent-id>');
      const read = [ownScope].concat(getSelectedScopes('ca-read-scopes'));
      const write = [ownScope].concat(getSelectedScopes('ca-write-scopes'));
      const recallAll = document.getElementById('ca-recall-all')?.checked;
      const recall = recallAll ? read : [ownScope].concat(getSelectedScopes('ca-recall-scopes'));
      const capabilities = getSelectedCapabilities('ca');
      const preview = document.getElementById('ca-authority-preview');
      if (!preview) return;
      preview.innerHTML = '<strong>What this agent will be able to do</strong>' +
        '<ul style="margin:6px 0 0 18px">' +
        '<li>Read: <code>' + escapeHtml(read.join(', ')) + '</code></li>' +
        '<li>Write: <code>' + escapeHtml(write.join(', ')) + '</code></li>' +
        '<li>Searches by default: <code>' + escapeHtml(recall.join(', ')) + '</code></li>' +
        '<li>Permissions: ' + (capabilities.length ? escapeHtml(capabilities.map(function(c) { return CAPABILITY_NAMES[c] || c; }).join(', ')) : 'none yet') + '</li>' +
        '</ul>';
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
      const toAgent = ev.target.closest('[data-agent-to-agent]');
      if (toAgent) { ev.preventDefault(); changeToAgent(); return; }
      const view = ev.target.closest('[data-agent-view]');
      if (view) { ev.preventDefault(); viewAgent(view.dataset.agentView); return; }
      const deactivate = ev.target.closest('[data-agent-deactivate]');
      if (deactivate) { ev.preventDefault(); deactivateAgent(deactivate.dataset.agentDeactivate); return; }
      const reactivate = ev.target.closest('[data-agent-reactivate]');
      if (reactivate) { ev.preventDefault(); reactivateAgent(reactivate.dataset.agentReactivate); return; }
      const keys = ev.target.closest('[data-agent-keys]');
      if (keys) { ev.preventDefault(); openKeys(keys.dataset.agentKeys); return; }
      const revoke = ev.target.closest('[data-key-revoke]');
      if (revoke) { ev.preventDefault(); revokeKey(revoke.dataset.keyAgent, revoke.dataset.keyRevoke, revoke.dataset.keyLabel); return; }
      const rename = ev.target.closest('[data-key-rename]');
      if (rename) { ev.preventDefault(); startKeyRename(rename); return; }
      const renameSave = ev.target.closest('[data-key-rename-save]');
      if (renameSave) { ev.preventDefault(); saveKeyRename(); return; }
      const renameCancel = ev.target.closest('[data-key-rename-cancel]');
      if (renameCancel) { ev.preventDefault(); cancelKeyRename(); return; }
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
      if (!await confirmDialog('Deactivate this agent? It will no longer be able to authenticate.')) return;
      const j = await apiFetch('/api/agents/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('Agent deactivated'); refreshAgents(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function reactivateAgent(id) {
      if (!await confirmDialog('Reactivate this agent?')) return;
      const j = await apiFetch('/api/agents/' + id + '/activate', { method: 'POST' });
      if (j.ok) { showToast('Agent reactivated'); refreshAgents(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function purgeAgent(id) {
      const typed = prompt('PERMANENTLY DELETE the agent "' + id + '"? Its keys stop working, and its private memory, anything stored in its private workspace (credentials, connections) and its tasks are deleted. This cannot be undone.\\n\\nType the agent ID to confirm:');
      if (typed === null) return;
      if (typed.trim() !== id) { showToast('That is not the agent ID. Nothing was deleted.', 'warning'); return; }
      const j = await apiFetch('/api/agents/' + id + '/purge', { method: 'POST' });
      if (j.ok) { 
        showToast('Agent deleted', 'success'); 
        refreshAgents(); 
      } else { 
        showToast(j.error?.message || 'Failed to delete agent', 'danger'); 
      }
    }
    let keysAgent = null;
    let keyRename = null;
    function keyTime(value) { return value ? new Date(value).toLocaleString() : 'never'; }
    async function openKeys(id, justAdded) {
      keysAgent = id;
      const j = await apiFetch('/api/agents/' + encodeURIComponent(id) + '/keys');
      if (!j.ok) { showToast((j.error && j.error.message) || 'Could not load the keys', 'danger'); return; }
      const rows = j.data.keys.map(function(k) {
        return '<tr><td data-label="Label"><strong>' + escapeHtml(k.label) + '</strong>' +
          (k.key_hint ? '<br><code>ac_sk_…' + escapeHtml(k.key_hint) + '</code>' : '') + '</td>' +
          '<td data-label="Created">' + escapeHtml(keyTime(k.created_at)) + '</td>' +
          '<td data-label="Last used">' + escapeHtml(keyTime(k.last_used_at)) + '</td>' +
          '<td><button type="button" class="btn btn-sm btn-secondary" data-key-rename="' + escapeHtml(k.id) +
          '" data-key-agent="' + escapeHtml(id) + '" data-key-label="' + escapeHtml(k.label) + '">Edit</button> ' +
          '<button type="button" class="btn btn-sm btn-danger" data-key-agent="' + escapeHtml(id) + '" data-key-revoke="' + escapeHtml(k.id) +
          '" data-key-label="' + escapeHtml(k.label) + '">Revoke</button></td></tr>';
      }).join('');
      const added = justAdded
        ? '<div class="alert alert-danger"><strong>Copy this key now: it is not shown again.</strong> (' + escapeHtml(justAdded.label) + ')</div>' +
          '<div class="api-key-display"><code>' + escapeHtml(justAdded.key) + '</code></div>' +
          '<button class="copy-btn" data-copy-value="' + escapeHtml(justAdded.key) + '">Copy</button>'
        : '';
      document.getElementById('keys-modal-title').textContent = 'Keys for ' + id;
      document.getElementById('keys-modal-body').innerHTML = added +
        '<p class="text-muted">Each tool or machine can have its own key. The label is your note about what the key is for; you can change it later. Revoking one key does not affect the others.</p>' +
        (rows
          ? '<table class="data-table"><thead><tr><th>Label</th><th>Created</th><th>Last used</th><th></th></tr></thead><tbody>' + rows + '</tbody></table>'
          : '<p>This agent has no keys yet. Add one below, or generate one on the Connect page.</p>') +
        '<form id="add-key-form" class="form-row" style="margin-top:12px"><label for="add-key-label">New key label</label>' +
        '<input id="add-key-label" class="form-input" maxlength="60" placeholder="Laptop, CI runner" required>' +
        '<button type="submit" class="btn">Add key</button></form>';
      document.getElementById('add-key-form').addEventListener('submit', async function(e) {
        e.preventDefault();
        const label = document.getElementById('add-key-label').value.trim();
        if (!label) return;
        const r = await apiFetch('/api/agents/' + encodeURIComponent(id) + '/keys', { method: 'POST', body: JSON.stringify({ label: label }) });
        if (r.ok) { openKeys(id, { key: r.data.api_key, label: r.data.key.label }); }
        else { showToast((r.error && r.error.message) || 'Could not add a key', 'danger'); }
      });
      openModal('keys-modal');
    }
    async function revokeKey(agentId, keyId, label) {
      if (!await confirmDialog('Revoke the key "' + label + '"? Anything using it stops working. The other keys of this agent are not affected.')) return;
      const j = await apiFetch('/api/agents/' + encodeURIComponent(agentId) + '/keys/' + encodeURIComponent(keyId), { method: 'DELETE' });
      if (j.ok) { showToast('Key revoked'); openKeys(agentId); }
      else { showToast((j.error && j.error.message) || 'Could not revoke the key', 'danger'); }
    }
    function startKeyRename(button) {
      const row = button.closest('tr');
      if (!row) return;
      if (keyRename) cancelKeyRename();  // only one label is edited at a time
      const cell = row.cells[0];
      const label = button.dataset.keyLabel || '';
      keyRename = { cell: cell, button: button, agentId: button.dataset.keyAgent, keyId: button.dataset.keyRename, html: cell.innerHTML };
      button.style.display = 'none';
      cell.innerHTML =
        '<input type="text" class="form-input" maxlength="60" value="' + escapeHtml(label) + '" data-key-rename-input>' +
        '<div style="margin-top:6px">' +
        '<button type="button" class="btn btn-sm" data-key-rename-save>Save</button> ' +
        '<button type="button" class="btn btn-sm btn-secondary" data-key-rename-cancel>Cancel</button></div>';
      const input = cell.querySelector('[data-key-rename-input]');
      input.focus();
      input.select();
      input.addEventListener('keydown', function(e) {
        if (e.key === 'Enter') { e.preventDefault(); saveKeyRename(); }
        else if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelKeyRename(); }
      });
    }
    function cancelKeyRename() {
      if (!keyRename) return;
      const state = keyRename;
      keyRename = null;
      state.cell.innerHTML = state.html;
      state.button.style.display = '';
    }
    async function saveKeyRename() {
      if (!keyRename) return;
      const state = keyRename;
      const input = state.cell.querySelector('[data-key-rename-input]');
      const label = (input.value || '').trim();
      if (!label) { showToast('Give the key a label, such as the machine or tool that uses it', 'danger'); return; }
      const j = await apiFetch('/api/agents/' + encodeURIComponent(state.agentId) + '/keys/' + encodeURIComponent(state.keyId), { method: 'PATCH', body: JSON.stringify({ label: label }) });
      if (!j.ok) { showToast((j.error && j.error.message) || 'Could not save the label', 'danger'); return; }
      keyRename = null;
      const overlay = document.getElementById('keys-modal');
      if (overlay) overlay.dataset.dirty = '';
      showToast('Key label saved');
      openKeys(state.agentId);
    }
    function setAgentModalReadOnly(readOnly) {
      agentModalReadOnly = Boolean(readOnly);
      const saveButton = document.getElementById('edit-agent-save');
      const modalTitle = document.getElementById('edit-agent-modal-title');
      const form = document.getElementById('edit-agent-form');
      if (modalTitle) {
        const what = editEntryIsService ? 'Service' : 'Agent';
        modalTitle.textContent = readOnly ? 'View ' + what : 'Edit ' + what;
      }
      if (saveButton) {
        saveButton.style.display = readOnly ? 'none' : '';
      }
      const toAgentButton = document.getElementById('edit-to-agent');
      if (toAgentButton) {
        toAgentButton.style.display = readOnly ? 'none' : '';
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
      editEntryIsService = (a.kind || rowKind(a.id)) === 'service';
      setEditServiceMode(editEntryIsService);
      document.getElementById('edit-agent-id').textContent = a.id;
      document.getElementById('edit-display-name').value = a.display_name || '';
      document.getElementById('edit-description').value = a.description || '';
      document.getElementById('edit-owner').textContent = a.owner_user_id || '-';
      document.getElementById('edit-default-user').textContent = a.default_user_id || a.owner_user_id || '-';
      setSelectedCapabilities('edit', a.capabilities || []);
      document.getElementById('edit-inherit-user-bindings').checked = !!a.inherit_user_bindings;
      syncRole('edit');
      const readScopes = JSON.parse(a.read_scopes_json || '[]');
      const writeScopes = JSON.parse(a.write_scopes_json || '[]');
      const noEffect = document.getElementById('edit-no-effect');
      if (noEffect) {
        noEffect.innerHTML = (a.no_effect || []).map(function(n) { return '<div>' + escapeHtml(n.message) + '</div>'; }).join('');
        noEffect.style.display = (a.no_effect || []).length ? '' : 'none';
      }
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
        if (prefix === 'ca') setServiceChoice(role === 'service');
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
        const errorBox = document.getElementById('edit-agent-error');
        if (errorBox) { errorBox.style.display = 'none'; errorBox.textContent = ''; }
        const ownScope = 'agent:' + id;
        const body = {
          display_name: document.getElementById('edit-display-name').value,
          description: document.getElementById('edit-description').value,
        };
        if (!editEntryIsService) {
          // A service has one permission and no workspaces, so only its name and description are sent.
          body.read_scopes = getSelectedScopes('edit-read-scopes');
          body.write_scopes = getSelectedScopes('edit-write-scopes');
          body.capabilities = getSelectedCapabilities('edit');
          body.inherit_user_bindings = document.getElementById('edit-inherit-user-bindings').checked;
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
        }
        const j = await apiFetch('/api/agents/' + id, { method: 'PUT', body: JSON.stringify(body) });
        if (j.ok) { showToast(editEntryIsService ? 'Service updated' : 'Agent updated'); closeModal('edit-agent-modal'); refreshAgents(); }
        else { showEditError(j.error.message || 'Failed'); }
      }

      // A service becomes an agent again: one change, nothing else to fill in.
      async function changeToAgent() {
        const id = document.getElementById('edit-agent-id').textContent;
        const errorBox = document.getElementById('edit-agent-error');
        if (errorBox) { errorBox.style.display = 'none'; errorBox.textContent = ''; }
        const j = await apiFetch('/api/agents/' + id, { method: 'PUT', body: JSON.stringify({ kind: 'agent' }) });
        if (j.ok) { location.href = '/agents'; return; }
        showEditError((j.error && j.error.message) || 'Could not change this service to an agent');
      }

      async function createAgent(e) {
        e.preventDefault();
        const errorBox = document.getElementById('create-agent-error');
          if (errorBox) {
            errorBox.style.display = 'none';
            errorBox.textContent = '';
          }
        const agentId = normalizeAgentId(document.getElementById('ca-id').value);
        const isService = selectedRole('ca') === 'service';
        const body = {
          id: agentId,
          display_name: document.getElementById('ca-display-name').value.trim(),
          description: document.getElementById('ca-description').value.trim(),
          capabilities: isService ? ['credentials_resolve'] : getSelectedCapabilities('ca'),
          inherit_user_bindings: isService ? false : document.getElementById('ca-inherit-user-bindings').checked,
          kind: isService ? 'service' : 'agent',
        };
        if (!isService) {
          body.read_scopes = getSelectedScopes('ca-read-scopes');
          body.write_scopes = getSelectedScopes('ca-write-scopes');
          document.querySelectorAll('#create-agent-form [data-simple-workspace]:checked').forEach(function(box) {
            const scope = box.dataset.simpleWorkspace;
            if (!body.read_scopes.includes(scope)) body.read_scopes.push(scope);
            if (!body.write_scopes.includes(scope)) body.write_scopes.push(scope);
          });
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
        }

        try {
          const j = await apiFetch('/api/agents', { method: 'POST', body: JSON.stringify(body) });
          if (j.ok) {
            closeModal('create-agent-modal');
            if (isService) {
              // A service belongs with the other services, not with the agents.
              location.href = '/services';
              return;
            }
            showToast('Agent created');
            document.getElementById('rotate-modal-title').textContent = 'Agent created';
            document.getElementById('rotate-modal-body').innerHTML =
              '<div class="alert alert-success">Agent created successfully.</div>' +
              '<p class="text-muted">Generate a one-time connection key and tool config from the Connect page when you are ready to connect this agent.</p>' +
              '<a class="btn" href="/integrations?agent_id=' + encodeURIComponent(j.data.agent.id) + '">Connect this agent</a>';
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
      document.addEventListener('DOMContentLoaded', function() {
        createAuthorityPreview(); updateAccessSummary('ca');
        // /agents?edit=<id> opens that agent's role, for links from other pages.
        const wanted = new URLSearchParams(location.search).get('edit');
        if (wanted) editAgent(wanted);
      });
    </script>"""
    js = js.replace("__IS_ADMIN__", str(is_admin).lower()).replace("__ROLE_PRESETS__", json.dumps(ROLE_PRESETS))
    js = js.replace("__AGENT_ROWS__", row_data)
    js = js.replace("__ACCESS_WORKSPACES__", workspace_data)
    # The Access dialogs live in their own file, loaded the same way as the page's other scripts.
    js = js + '\n<script src="/static/js/agent_access.js?v=20261117a"></script>'

    page_title = "Services" if show_services else "Agents"
    create_button = (
        '<button class="btn" onclick="openCreateService()">+ Create service</button>'
        if show_services
        else '<button class="btn" onclick="openModal(\'create-agent-modal\')">+ Create Agent</button>'
    )
    access_card = (
        ""
        if show_services
        else (
            '<div class="card"><h2>Agent Access</h2><p class="text-muted access-summary">Each agent has its own private '
            "memory and can read its owner's. Add a workspace to share memory. Use <strong>Edit</strong> to give an "
            "agent more; create a new one only for a separate tool.</p></div>"
        )
    )

    # The Access dialog of the page: what one agent can do and reach, or what one service can fetch.
    agent_access_dialog = """
    <!-- Access Modal: what one agent can do and reach -->
    <div class="modal-overlay" id="access-agent-modal" style="display:none">
      <div class="modal modal-wide">
        <h2 id="access-agent-title">What <span id="access-agent-name"></span> can do and reach</h2>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Workspaces</h3>
          <p class="form-hint">The shared places this agent can read and write. Adding or removing one shows what it would open or close before anything is saved.</p>
          <div id="access-agent-workspaces"></div>
          <div id="access-agent-workspace-preview"></div>
          <div class="form-group" style="margin-top:12px">
            <label for="access-agent-workspace-pick">Add a workspace</label>
            <select id="access-agent-workspace-pick"></select>
            <label class="checkbox-label"><input type="checkbox" id="access-agent-workspace-read" checked> Read</label>
            <label class="checkbox-label"><input type="checkbox" id="access-agent-workspace-write"> Write</label>
            <button type="button" class="btn btn-sm btn-secondary" id="access-agent-workspace-add">Add</button>
          </div>
        </div>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Capabilities</h3>
          <p class="form-hint">The connections this agent can reach, and whether it may run them.</p>
          <div id="access-agent-capabilities"></div>
        </div>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Credentials it can reference</h3>
          <p class="form-hint">The stored secrets this agent may ask for by name. It never receives the value itself.</p>
          <div id="access-agent-credentials"></div>
        </div>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Keys</h3>
          <div id="access-agent-keys"></div>
        </div>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Permissions</h3>
          <div id="access-agent-permissions"></div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" id="access-agent-cancel">Cancel</button>
          <button type="button" class="btn" id="access-agent-save">Save</button>
        </div>
      </div>
    </div>
    """

    service_access_dialog = """
    <!-- Access Modal: what one service can fetch -->
    <div class="modal-overlay" id="access-service-modal" style="display:none">
      <div class="modal modal-wide">
        <h2 id="access-service-title">What <span id="access-service-name"></span> can fetch</h2>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Secrets it can fetch</h3>
          <p class="form-hint">A service receives the secret value itself.</p>
          <div id="access-service-secrets"></div>
          <div class="form-group" style="margin-top:12px">
            <label for="access-service-secret-pick">Add a secret</label>
            <select id="access-service-secret-pick"></select>
            <label class="checkbox-label"><input type="checkbox" id="access-service-secret-write"> Allow it to write the value back</label>
            <div class="form-group">
              <label for="access-service-secret-expires">Access ends on</label>
              <input type="date" id="access-service-secret-expires">
            </div>
            <button type="button" class="btn btn-sm btn-secondary" id="access-service-secret-add">Add</button>
          </div>
        </div>
        <div class="form-section">
          <h3 class="sub-heading form-section-title">Keys</h3>
          <div id="access-service-keys"></div>
        </div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" onclick="closeModal('access-service-modal')">Close</button>
        </div>
      </div>
    </div>
    """

    access_dialog_html = service_access_dialog if show_services else agent_access_dialog

    return render_page(
        page_title,
        f"""
    <div class="page-header"><h1>{page_title}</h1><div class="page-actions">
        {create_button}
    </div></div>
    {access_card}

    {"" if show_services else switch_html}
    {services_note}

    <div class="card">
      <h2>{list_title}</h2>
<table class="stack-phone"><thead><tr><th>{list_column}</th><th>Status</th><th>Access</th><th>Created</th><th class="actions-cell">Actions</th></tr></thead>
        <tbody>{rows or empty_row}</tbody></table>
    </div>

    <!-- Create Agent Modal -->
    <div class="modal-overlay" id="create-agent-modal" style="display:none">
      <div class="modal modal-wide">
        <h2>Create Agent</h2>
        <p class="form-hint" style="margin-bottom:12px">Does an existing agent already do this job? Then use <strong>Edit</strong> on its row to give it what it needs. A new agent is for a separate tool or identity.</p>
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
                <p class="form-hint">The name shown in lists.</p>
              </div>
            </div>
            <div class="form-group">
              <label>Description</label>
              <input type="text" id="ca-description" placeholder="What is this agent for?">
              <p class="form-hint">A reminder of what this agent is for.</p>
            </div>
          </div>
          <div id="ca-capability-controls">{capability_create_html}</div>
          <p id="ca-service-note" class="form-hint" hidden>A service only fetches secrets, so it has one permission and no workspaces.</p>
          <div id="ca-access">{create_access_html}</div>
          <div class="form-group" id="ca-access-reset">
            <button type="button" class="btn btn-sm btn-secondary" onclick="applyLeastPrivilegedPreset()">Reset to private access only</button>
            <span class="form-hint">Clears every extra workspace; the agent keeps its private memory and its owner's read access.</span>
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
        <h2><span id="edit-agent-modal-title">Edit Agent</span>: <span id="edit-agent-id"></span></h2>
        <div id="edit-no-effect" class="alert alert-warning" style="display:none"></div>
        <div id="edit-agent-error" class="alert alert-danger" style="display:none"></div>
        <form id="edit-agent-form" onsubmit="submitEditAgent(event)">
          <div class="form-section">
            <div class="form-row">
              <div class="form-group">
                <label>Display name</label>
                <input type="text" id="edit-display-name">
                <p class="form-hint">The name shown in lists.</p>
              </div>
              <div class="form-group">
                <label>Description</label>
                <input type="text" id="edit-description">
                <p class="form-hint">A reminder of what this agent is for.</p>
              </div>
            </div>
            <p class="form-hint" id="edit-owner-line">Owner: <span id="edit-owner"></span><span id="edit-default-user" hidden></span></p>
          </div>
          <div id="edit-capability-controls">{capability_edit_html}</div>
          <div id="edit-access">{edit_access_html}</div>
          <div id="edit-service-note" hidden>
            <p class="form-hint">A service only fetches secrets, so it has one permission and no workspaces.</p>
            <button type="button" class="btn btn-secondary" id="edit-to-agent" data-agent-to-agent="1">Change to agent</button>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-agent-modal')">Cancel</button>
            <button id="edit-agent-save" type="submit" class="btn">Save Changes</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Keys Modal -->
    <div class="modal-overlay" id="keys-modal" style="display:none">
      <div class="modal">
        <h2 id="keys-modal-title">Keys</h2>
        <div id="keys-modal-body"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('keys-modal'); refreshAgents();">Done</button>
        </div>
      </div>
    </div>

    <!-- Rotate Key Modal -->
    <div class="modal-overlay" id="rotate-modal" style="display:none">
      <div class="modal">
        <h2 id="rotate-modal-title">Agent</h2>
        <div id="rotate-modal-body"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('rotate-modal'); refreshAgents();">Done</button>
        </div>
      </div>
    </div>

    {access_dialog_html}
    """,
        "/services" if show_services else "/agents",
        js,
        session=session,
    )
