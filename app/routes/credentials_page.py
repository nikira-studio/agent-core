"""Credentials dashboard page. Split from connectors_page.py — see
private/dashboard-split-plan.md."""

from fastapi import APIRouter, Request, Depends

from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer
from app.services import credential_service, workspace_service
from app.services.agent_service import list_agents
from app.routes.dashboard_shared import render_page, require_auth, escape_html, get_icon

router = APIRouter()


@router.get("/credentials")
async def credentials_page(request: Request, session: dict = Depends(require_auth)):
    ctx = build_user_context(session)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    credential_entries = [
        e
        for e in (credential_service.list_credentials(limit=500) or [])
        if enforcer.can_read(e.get("scope", ""))
    ]

    workspaces = (
        workspace_service.list_workspaces()
        if ctx.is_admin
        else workspace_service.list_workspaces(owner_user_id=ctx.user_id)
    )
    agents = list_agents() if ctx.is_admin else list_agents(owner_user_id=ctx.user_id)

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

    credential_rows = ""
    for e in credential_entries:
        credential_rows += f"""
        <tr data-credential-id="{e["id"]}">
          <td>{escape_html(e.get("name", ""))}</td>
          <td><code>{escape_html(e.get("scope", ""))}</code></td>
          <td><code>{escape_html(e.get("reference_name", ""))}</code></td>
          <td class='actions-cell'>
            <button type='button' class='btn btn-sm btn-secondary' onclick='editCredential("{e["id"]}")'>Edit</button>
            <button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick='deleteCredential("{e["id"]}")' title='Delete credential' aria-label='Delete credential'>{get_icon("delete")}</button>
          </td>
        </tr>"""

    if credential_entries:
        credentials_html = f"""
        <table><thead><tr><th>Name</th><th>Scope</th><th>Reference</th><th class='actions-cell'>Actions</th></tr></thead>
        <tbody>{credential_rows}</tbody></table>"""
    else:
        credentials_html = "<div class='empty'>No credentials yet.</div>"

    body = f"""
    <div class="page-header">
      <div>
        <h1>Credentials</h1>
        <p class="text-muted" style="max-width:640px;margin-top:8px">
          Encrypted secrets used by connector bindings and agents. Credentials are scoped and never returned raw via the API.
        </p>
      </div>
      <div class="page-actions">
        <button class="btn" onclick="openModal('create-credential-modal')">+ New Credential</button>
      </div>
    </div>

    <div class="card">
      <div id="credentials-list">{credentials_html}</div>
    </div>

    <!-- Create Credential Modal -->
    <div class="modal-overlay" id="create-credential-modal" style="display:none">
      <div class="modal">
        <h3>New Credential</h3>
        <form id="create-credential-form" onsubmit="createCredential(event)">
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="credential-name" placeholder="e.g. service-token" autocomplete="off" required>
          </div>
          <div class="form-group">
            <label>Label</label>
            <input type="text" id="credential-label" placeholder="e.g. Service token" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Scope *</label>
            <select id="credential-scope" required>
              <option value="">Select scope...</option>
              {scope_options}
            </select>
          </div>
          <div class="form-group">
            <label>Secret Value *</label>
            <input type="password" id="credential-value" autocomplete="new-password" required>
          </div>
          <button type="submit" class="btn btn-primary">Create Credential</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('create-credential-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Edit Credential Modal -->
    <div class="modal-overlay" id="edit-credential-modal" style="display:none">
      <div class="modal">
        <h3>Edit Credential</h3>
        <form id="edit-credential-form" onsubmit="submitEditCredential(event)">
          <input type="hidden" id="edit-credential-id">
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="edit-credential-name" autocomplete="off" required>
          </div>
          <div class="form-group">
            <label>Label</label>
            <input type="text" id="edit-credential-label" autocomplete="off">
          </div>
          <div class="form-group">
            <label>Scope</label>
            <input type="text" id="edit-credential-scope" autocomplete="off" disabled>
          </div>
          <div class="form-group">
            <label>Replace Secret Value</label>
            <input type="password" id="edit-credential-value" autocomplete="new-password" placeholder="Leave blank to keep current value">
          </div>
          <button type="submit" class="btn btn-primary">Save Credential</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('edit-credential-modal')">Cancel</button>
        </form>
      </div>
    </div>
    """

    extra_js = '<script src="/static/js/credentials.js?v=20260626"></script>'

    return render_page("Credentials", body, "/credentials", extra_js, session=session)
