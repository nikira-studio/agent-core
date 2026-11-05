"""Credentials dashboard page."""

from fastapi import APIRouter, Request, Depends

from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer
from app.services import credential_service, workspace_service
from app.services.agent_service import list_agents
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    get_icon,
    local_dt,
)
from app.database import get_db
from app.services.credential_service import is_expired
from app.time_utils import parse_utc_datetime, utc_now

router = APIRouter()

# How close to expiry counts as worth warning about.
EXPIRY_WARNING_DAYS = 14


def _expiry_cell(expires_at) -> str:
    """What an operator needs to know about a credential's lifetime at a glance.

    An expired credential does not announce itself: the connector that depends
    on it simply starts failing, and the reason is two pages away. The backend
    has tracked `expires_at` all along; this is the first place it is shown.
    """
    if not expires_at:
        return "<span class='text-muted'>—</span>"
    try:
        expiry = parse_utc_datetime(expires_at)
    except (ValueError, TypeError):
        return f"<span class='text-muted'>{escape_html(str(expires_at))}</span>"

    days = (expiry - utc_now()).days
    # local_dt already returns an element the browser localises; escaping it
    # again renders the markup as text.
    shown = local_dt(expires_at, style="date")
    if days < 0:
        return f"<span class='badge badge-danger'>expired</span> <span class='text-muted'>{shown}</span>"
    if days <= EXPIRY_WARNING_DAYS:
        label = "expires today" if days == 0 else f"{days}d left"
        return f"<span class='badge badge-warning'>{label}</span> <span class='text-muted'>{shown}</span>"
    return f"<span class='text-muted'>{shown}</span>"


@router.get("/credentials")
def credentials_page(request: Request, session: dict = Depends(require_auth)):
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

    # Who can use each credential, for admins: the grants are theirs to manage.
    holders: dict[str, list[str]] = {}
    service_counts: dict[str, int] = {}
    if ctx.is_admin:
        agent_names = {a["id"]: a.get("display_name") or a["id"] for a in agents}
        with get_db() as conn:
            for g in conn.execute(
                "SELECT entry_id, consumer_agent_id, expires_at FROM credential_consumer_grants WHERE revoked_at IS NULL"
            ).fetchall():
                if not is_expired({"expires_at": g["expires_at"]}):
                    holders.setdefault(g["entry_id"], []).append(
                        agent_names.get(g["consumer_agent_id"], g["consumer_agent_id"])
                    )
            for r in conn.execute(
                "SELECT credential_id, COUNT(*) AS n FROM connector_bindings WHERE credential_id IS NOT NULL GROUP BY credential_id"
            ).fetchall():
                service_counts[r["credential_id"]] = r["n"]

    def used_by_cell(entry_id: str) -> str:
        names = holders.get(entry_id, [])
        chips = "".join(
            f"<span class='badge badge-info chip-trunc' title='{escape_html(n)}'>{escape_html(n)}</span> " for n in names[:4]
        )
        more = f"<span class='text-muted'>+{len(names) - 4} more</span> " if len(names) > 4 else ""
        n_services = service_counts.get(entry_id, 0)
        services = f"<div class='text-muted' style='font-size:0.8rem'>{n_services} service{'s' if n_services != 1 else ''}</div>" if n_services else ""
        return f"{chips}{more}{services}" if (names or services) else "<span class='text-muted'>no one yet</span>"

    credential_rows = ""
    for e in credential_entries:
        origin = " · ".join(
            part
            for part in (
                f"added {local_dt(e.get('created_at'), style='date')}"
                if e.get("created_at")
                else "",
                f"by {escape_html(e['created_by'])}" if e.get("created_by") else "",
            )
            if part
        )
        used_cell = access_button = ""
        if ctx.is_admin:
            used_cell = "<td data-label='Used by'>" + used_by_cell(e["id"]) + "</td>"
            access_button = (
                "<button type='button' class='btn btn-sm btn-secondary' data-credential-access='"
                + escape_html(e["id"]) + "'>Access</button>"
            )
        credential_rows += f"""
        <tr data-credential-id="{e["id"]}">
          <td>{escape_html(e.get("name", ""))}
            <div class='text-muted' style='font-size:0.8rem'>{origin}</div>
            <button type='button' class='btn btn-sm btn-link' data-copy-reference="{escape_html(e.get("reference_name", ""))}" title="Copy {escape_html(e.get("reference_name", ""))}">Copy reference</button></td>
          <td data-label='Scope'><code class='scope-code'>{escape_html(e.get("scope", ""))}</code></td>
          <td data-label='Expires'>{_expiry_cell(e.get("expires_at"))}</td>
          {used_cell}
          <td class='actions-cell'>
            {access_button}
            <button type='button' class='btn btn-sm btn-secondary' data-credential-edit="{escape_html(e["id"])}">Edit</button>
            <button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-credential-delete="{escape_html(e["id"])}" title='Delete credential' aria-label='Delete credential'>{get_icon("delete")}</button>
          </td>
        </tr>"""

    from app.services import credential_request_service

    pending_requests = [
        r for r in credential_request_service.list_pending() if enforcer.can_write(r["scope"])
    ]
    requests_html = ""
    if pending_requests:
        request_rows = "".join(
            f"""
        <tr>
          <td>{escape_html(r["name"])}
            <div class='text-muted' style='font-size:0.8rem'>asked by {escape_html(r["requested_by_agent_id"])} · {local_dt(r["created_at"], style="date")}</div></td>
          <td><code>{escape_html(r["scope"])}</code></td>
          <td>{escape_html(r["purpose"])}</td>
          <td class='actions-cell'>
            <button type='button' class='btn btn-sm' data-request-fill="{escape_html(r["id"])}" data-request-name="{escape_html(r["name"])}">Provide secret</button>
            <button type='button' class='btn btn-sm btn-secondary' data-request-cancel="{escape_html(r["id"])}">Decline</button>
          </td>
        </tr>"""
            for r in pending_requests
        )
        requests_html = f"""
    <div class="card" id="credential-requests">
      <h3>Requested by agents</h3>
      <p class="text-muted">An agent needs these to continue. The value goes straight into the vault; the agent only ever receives the reference.</p>
      <table><thead><tr><th>Name</th><th>Scope</th><th>What it's for</th><th class='actions-cell'>Actions</th></tr></thead>
      <tbody>{request_rows}</tbody></table>
    </div>"""

    used_header = "<th>Used by</th>" if ctx.is_admin else ""
    if credential_entries:
        credentials_html = f"""
        <table class='stack-table'><thead><tr><th>Name</th><th>Scope</th><th>Expires</th>{used_header}<th class='actions-cell'>Actions</th></tr></thead>
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

    {requests_html}
    <div class="card">
      <div id="credentials-list">{credentials_html}</div>
    </div>

    <!-- Who can use a credential -->
    <div class="modal-overlay" id="access-modal" style="display:none">
      <div class="modal modal-wide">
        <h3>Who can use <span id="access-name"></span></h3>
        <p class="form-hint" style="margin-bottom:12px">One credential per service: give it to each agent that needs it instead of storing it again. Use a separate credential only for a different account or an outside service.</p>
        <h4 class="form-section-title">Agents with access</h4>
        <div id="access-grants"></div>
        <div class="form-group">
          <label for="access-agent">Give access to</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select id="access-agent" style="flex:1"></select>
            <button type="button" class="btn btn-sm" id="access-add">Add</button>
          </div>
          <p class="form-hint">An agent can be given a credential when its role includes reading credential values (the Service agent role).</p>
          <details id="access-cannot" style="display:none"><summary class="text-muted">Can't receive it yet (<span id="access-cannot-count"></span>)</summary><div id="access-cannot-list"></div></details>
        </div>
        <h4 class="form-section-title">Services using it</h4>
        <div id="access-services"></div>
        <p class="form-hint" id="access-scope-readers"></p>
        <details class="access-details">
          <summary>Advanced</summary>
          <div class="form-group">
            <label for="access-expires">Access ends on</label>
            <input type="date" id="access-expires" style="max-width:200px">
            <p class="form-hint">Optional. Applies to the next agent you add. Leave empty for no end date.</p>
          </div>
          <div class="form-group">
            <label class="checkbox-label"><input type="checkbox" id="access-write"> Allow it to write the value back</label>
            <p class="form-hint">Off by default. Only for an agent that rotates this secret itself.</p>
          </div>
        </details>
        <div class="modal-footer"><button type="button" class="btn btn-secondary" onclick="closeAccess()">Close</button></div>
      </div>
    </div>

    <!-- One agent's credentials, opened from the Agents page -->
    <div class="modal-overlay" id="agent-access-modal" style="display:none">
      <div class="modal modal-wide">
        <h3>What <span id="agent-access-name"></span> can use</h3>
        <p class="form-hint">Give an agent a credential that is already stored here instead of storing it again.</p>
        <div id="agent-access-list"></div>
        <div class="form-group">
          <label for="agent-access-pick">Give access to</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select id="agent-access-pick" style="flex:1"></select>
            <button type="button" class="btn btn-sm" id="agent-access-add">Add</button>
          </div>
        </div>
        <div class="modal-footer"><button type="button" class="btn btn-secondary" onclick="closeAgentAccess()">Close</button></div>
      </div>
    </div>

    <!-- Fill Credential Request Modal -->
    <div class="modal-overlay" id="fill-request-modal" style="display:none">
      <div class="modal">
        <h3>Provide <span id="fill-request-name"></span></h3>
        <form id="fill-request-form" onsubmit="fillCredentialRequest(event)">
          <input type="hidden" id="fill-request-id">
          <div class="form-group">
            <label>Secret Value *</label>
            <input type="password" id="fill-request-value" autocomplete="new-password" required>
          </div>
          <div class="form-group">
            <label>Expires</label>
            <input type="date" id="fill-request-expires">
          </div>
          <button type="submit" class="btn btn-primary">Save to vault</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('fill-request-modal')">Cancel</button>
        </form>
      </div>
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
          <div class="form-group">
            <label>Expires</label>
            <input type="date" id="credential-expires">
            <p class="form-hint">Optional. Set it when the secret itself has an end date, so the dashboard can warn you before a connector starts failing.</p>
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
            <select id="edit-credential-scope" required>
              {scope_options}
            </select>
            <p class="form-hint">Moving a credential changes who can access the stored secret. Linked bindings stay in their current scope.</p>
          </div>
          <div class="form-group">
            <label>Replace Secret Value</label>
            <input type="password" id="edit-credential-value" autocomplete="new-password" placeholder="Leave blank to keep current value">
          </div>
          <div class="form-group">
            <label>Expires</label>
            <input type="date" id="edit-credential-expires">
            <p class="form-hint">Clear the field to remove the expiry.</p>
          </div>
          <button type="submit" class="btn btn-primary">Save Credential</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('edit-credential-modal')">Cancel</button>
        </form>
      </div>
    </div>
    """

    extra_js = '<script src="/static/js/credentials.js?v=20261105"></script>'

    return render_page("Credentials", body, "/credentials", extra_js, session=session)
