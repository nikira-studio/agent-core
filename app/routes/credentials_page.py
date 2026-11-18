"""Credentials dashboard page."""

import json

from fastapi import APIRouter, Request, Depends

from app.security.context import build_user_context
from app.security.scope_enforcer import ScopeEnforcer
from app.services import connector_service, credential_service, reach_service, workspace_service
from app.services.agent_service import list_agents
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    get_icon,
    local_dt,
    Owners,
)
from app.database import get_db
from app.services.credential_service import is_expired
from app.time_utils import parse_utc_datetime, utc_now

router = APIRouter()

# How close to expiry counts as worth warning about.
EXPIRY_WARNING_DAYS = 14


_SCOPE_KIND_TAG = {"workspace": "workspace", "user": "personal", "agent": "agent"}


REFERENCE_NOTE = (
    "They can use it wherever Agent Core makes a connection with it. "
    "Agent Core does not show them the secret itself."
)


def connection_owner_name(binding_id: str, connections: dict[str, str] | None) -> str:
    """The connection a credential belongs to, in plain words: its name, or that it is gone.

    A credential that belongs to a connection is never shown as the raw scope, and never as the connection's id.
    """
    if connections is None:
        return "A connection"
    name = connections.get(binding_id)
    return f"Connection {name}" if name else "A deleted connection"


def scope_html(
    scope: str,
    connections: dict[str, str] | None = None,
    owners: Owners | None = None,
) -> str:
    """The owner of a credential, with a small tag for which kind of owner it is.

    The name is a name, never an id, and it never splits inside itself (web-frontend stays whole); the tag sits
    beside or under it.
    """
    kind, sep, name = scope.partition(":")
    if not sep:
        return f"<span>{escape_html(scope)}</span>"
    if kind == "capability":
        # A credential that belongs to a connection: the connection's name, and no raw scope anywhere.
        return f"<span class='scope-name'>{escape_html(connection_owner_name(name, connections))}</span>"
    tag = _SCOPE_KIND_TAG.get(kind, kind)
    display = owners.display_name(scope) if owners else name
    title = owners.words(scope) if owners else scope
    return (
        f"<span class='scope-name'>{escape_html(display)}</span> "
        f"<span class='scope-kind' title='{escape_html(title)}'>{escape_html(tag)}</span>"
    )


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
        is_person=ctx.actor_type != "agent",
    )

    credential_entries = [
        e
        for e in (credential_service.list_credentials(limit=500) or [])
        if enforcer.can_read(e.get("scope", ""))
    ]

    # The connections this person may see, by id: a credential that belongs to one is named after it.
    connections = {
        b["id"]: (b.get("name") or b["id"])
        for b in connector_service.list_bindings()
        if enforcer.can_read(b.get("scope", ""))
    }

    workspaces = (
        workspace_service.list_workspaces()
        if ctx.is_admin
        else workspace_service.list_workspaces(owner_user_id=ctx.user_id)
    )
    agents = list_agents() if ctx.is_admin else list_agents(owner_user_id=ctx.user_id)
    # The owners, in plain words: the workspaces, the people and the agents by name, never by id.
    owners = Owners(workspaces=workspaces, agents=agents)

    user_scope = f"user:{session['user_id']}"
    workspace_scope_opts = "".join(
        f'<option value="workspace:{p["id"]}">{escape_html(p.get("name") or p["id"])}</option>'
        for p in workspaces
    )
    agent_scope_opts = "".join(
        f'<option value="agent:{a["id"]}">{escape_html(a.get("display_name") or a["id"])}</option>'
        for a in agents
        if a.get("is_active")
    )
    scope_options = (
        f'<optgroup label="You"><option value="{user_scope}">Just me (personal)</option></optgroup>\n'
        f'<optgroup label="Workspaces">{workspace_scope_opts}</optgroup>\n'
        f'<optgroup label="Agents (private to one agent)">{agent_scope_opts}</optgroup>\n'
    )

    # Who can use each credential, for admins: the grants are theirs to manage.
    holders: dict[str, list[str]] = {}
    service_counts: dict[str, int] = {}
    # The agents that reach each credential, with the same answer the Access dialog gives: the agent contexts
    # are built once for the page, not once per credential.
    reaching: dict[str, list[dict]] = {}
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
        contexts = reach_service.agent_contexts()
        reach_names = reach_service.Names()
        for entry in credential_entries:
            reaching[entry["id"]] = reach_service.agents_reaching_credential(entry, contexts, reach_names)

    def used_by_cell(entry_id: str) -> str:
        """Always the same parts, in the same order: who was given the secret, who can reach it, connections.

        The dialog shows the same two groups under the same labels (credentials.js renderUsedBy mirrors this)."""
        names = holders.get(entry_id, [])
        chips = "".join(
            f"<span class='badge badge-info chip-trunc' title='{escape_html(n)}'>{escape_html(n)}</span> " for n in names[:4]
        )
        more = f"<span class='text-muted'>+{len(names) - 4} more</span> " if len(names) > 4 else ""
        given = (
            f"<div class='used-by-given'><span class='used-by-label'>Given the secret:</span> {chips}{more}</div>"
            if names
            else "<div class='used-by-given'><span class='used-by-label'>Given the secret:</span> <span class='text-muted'>no one</span></div>"
        )
        others = [a.get("display_name") or a.get("agent_id", "") for a in reaching.get(entry_id, [])]
        seen = ""
        if others:
            seen = (
                f"<div class='text-muted used-by-seen' style='font-size:0.8rem' title='{escape_html(', '.join(others))}. {escape_html(REFERENCE_NOTE)}'>"
                f"{len(others)} agent{'s' if len(others) != 1 else ''} can reach it</div>"
            )
        n_connections = service_counts.get(entry_id, 0)
        connections = f"<div class='text-muted' style='font-size:0.8rem'>{n_connections} connection{'s' if n_connections != 1 else ''}</div>" if n_connections else ""
        return f"{given}{seen}{connections}"

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
          <td>{escape_html(e.get("name", "")).replace("_", "_<wbr>")}
            <div class='text-muted' style='font-size:0.8rem'>{origin}</div>
            <button type='button' class='btn btn-sm btn-link' data-copy-reference="{escape_html(e.get("reference_name", ""))}" title="Copy {escape_html(e.get("reference_name", ""))}">Copy reference</button></td>
          <td data-label='Owner'><code class='scope-code'>{scope_html(e.get("scope", ""), connections, owners)}</code></td>
          <td data-label='Expires'>{_expiry_cell(e.get("expires_at"))}</td>
          {used_cell}
          <td class='actions-cell'>
            {access_button}
            <button type='button' class='btn btn-sm btn-secondary' data-credential-edit="{escape_html(e["id"])}">Edit</button>
            <button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-credential-delete="{escape_html(e["id"])}" title='Delete credential' aria-label='Delete credential'>{get_icon("delete")} Delete</button>
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
          <td><code class='scope-code'>{scope_html(r["scope"], connections, owners)}</code></td>
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
      <h2>Requested by agents</h2>
      <p class="text-muted">An agent needs these to continue. The value is stored encrypted; the agent only ever receives the reference.</p>
      <table><thead><tr><th>Name</th><th>Owner</th><th>What it's for</th><th class='actions-cell'>Actions</th></tr></thead>
      <tbody>{request_rows}</tbody></table>
    </div>"""

    used_header = "<th class='col-used'>Used by</th>" if ctx.is_admin else ""
    if credential_entries:
        credentials_html = f"""
        <table class='stack-table'><thead><tr><th>Name</th><th class='col-scope'>Owner</th><th class='col-expires'>Expires</th>{used_header}<th class='actions-cell col-actions'>Actions</th></tr></thead>
        <tbody>{credential_rows}</tbody></table>"""
    else:
        credentials_html = "<div class='empty'>No credentials yet.</div>"

    # The connection names, for the browser: a credential that belongs to a connection is shown with its name.
    connection_names_blob = (
        json.dumps(connections)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("'", "\\u0027")
    )

    body = f"""
    <div class="page-header">
      <div>
        <h1>Credentials</h1>
        <p class="text-muted" style="max-width:640px;margin-top:8px">
          Values are stored encrypted. Agents and services use them by reference. A signed-in person who can read a credential can show its value again from Edit, and each time is recorded in the audit log.
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

    <!-- The connections on this page, by id: a credential that belongs to one is shown with its name. -->
    <script>window.AC_CREDENTIAL_CONNECTIONS = {connection_names_blob};</script>

    <!-- Who can use a credential: one list, in three groups -->
    <div class="modal-overlay" id="access-modal" style="display:none">
      <div class="modal modal-wide">
        <h2>Who can use <span id="access-name"></span></h2>
        <p class="form-hint" style="margin-bottom:12px">Store each secret once. Let every connection or service that needs it use the same credential. Use a separate credential only for a different account or a different permission level.</p>

        <h3 class="sub-heading form-section-title">Connections that sign in with this</h3>
        <div id="access-connections"></div>
        <div class="form-group">
          <label for="access-connection-pick">Add a connection</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select id="access-connection-pick" style="flex:1"></select>
            <button type="button" class="btn btn-sm" id="access-connection-add" disabled>Add</button>
          </div>
          <p id="access-connection-empty" class="text-muted" style="display:none">No connection to add yet. <a href="/connectors">Create one under Capabilities</a>.</p>
        </div>

        <div style="display:flex;align-items:center;gap:12px;flex-wrap:wrap">
          <h3 class="sub-heading form-section-title" style="margin:0">Agents that can reach this</h3>
          <span class="text-muted">Owner: <span id="access-owner"></span></span>
          <button type="button" class="btn btn-sm btn-secondary" id="access-owner-change">Change owner</button>
        </div>
        <p class="form-hint" id="access-agents-hint">Agents get a reference, never the secret value; Agent Core fills it in when a tool runs.</p>
        <div id="access-agents"></div>
        <div id="access-owner-panel" style="display:none">
          <div class="form-group">
            <label for="access-owner-pick">New owner</label>
            <select id="access-owner-pick" style="max-width:340px">
              {scope_options}
            </select>
          </div>
          <div id="access-owner-preview"></div>
          <div style="display:flex;gap:8px">
            <button type="button" class="btn btn-primary" id="access-owner-save" disabled>Save</button>
            <button type="button" class="btn btn-secondary" id="access-owner-cancel">Cancel</button>
          </div>
        </div>

        <h3 class="sub-heading form-section-title">Services that receive this value</h3>
        <p class="form-hint">A service receives the secret value itself. An agent using a capability does not.</p>
        <div id="access-services"></div>
        <p id="access-service-empty" class="text-muted" style="display:none"><a href="/services">No service exists yet. Create one under Credentials &gt; Services.</a></p>
        <div class="form-group">
          <label for="access-service-pick">Give a service this secret</label>
          <select id="access-service-pick" style="max-width:340px"></select>
          <label class="checkbox-label"><input type="checkbox" id="access-write"> Allow it to write the value back</label>
          <p class="form-hint">Off by default. Only for a service that rotates this secret itself.</p>
          <label for="access-expires">Access ends on</label>
          <input type="date" id="access-expires" style="max-width:200px">
          <p class="form-hint">Optional. Leave empty for no end date.</p>
          <button type="button" class="btn btn-sm" id="access-service-add" disabled>Add</button>
        </div>

        <div class="modal-footer"><button type="button" class="btn btn-secondary" onclick="closeAccess()">Close</button></div>
      </div>
    </div>

    <!-- One agent's credentials, opened from the Agents page -->
    <div class="modal-overlay" id="agent-access-modal" style="display:none">
      <div class="modal modal-wide">
        <h2>What <span id="agent-access-name"></span> can use</h2>
        <p class="form-hint">Give a service a credential that is already stored here instead of storing it again.</p>
        <div id="agent-access-list"></div>
        <div class="form-group">
          <label for="agent-access-pick">Give access to</label>
          <div style="display:flex;gap:8px;align-items:center">
            <select id="agent-access-pick" style="flex:1"></select>
            <button type="button" class="btn btn-sm" id="agent-access-add" disabled>Add</button>
          </div>
        </div>
        <div class="modal-footer"><button type="button" class="btn btn-secondary" onclick="closeAgentAccess()">Close</button></div>
      </div>
    </div>

    <!-- Fill Credential Request Modal -->
    <div class="modal-overlay" id="fill-request-modal" style="display:none">
      <div class="modal">
        <h2>Provide <span id="fill-request-name"></span></h2>
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
          <button type="submit" class="btn btn-primary">Save</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('fill-request-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Create Credential Modal -->
    <div class="modal-overlay" id="create-credential-modal" style="display:none">
      <div class="modal">
        <h2>New Credential</h2>
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
            <label>Owner *</label>
            <select id="credential-scope" required>
              <option value="">Select where it lives...</option>
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
            <p class="form-hint">Optional. Set it when the secret itself has an end date, so the dashboard can warn you before a connection or service using it starts failing.</p>
          </div>
          <button type="submit" class="btn btn-primary">Create Credential</button>
          <button type="button" class="btn btn-secondary" onclick="closeModal('create-credential-modal')">Cancel</button>
        </form>
      </div>
    </div>

    <!-- Edit Credential Modal -->
    <div class="modal-overlay" id="edit-credential-modal" style="display:none">
      <div class="modal">
        <h2>Edit Credential</h2>
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
            <label>Owner</label>
            <select id="edit-credential-scope" required>
              {scope_options}
            </select>
            <p class="form-hint">Moving a credential changes who can access the stored secret. Linked connections stay where they are.</p>
          </div>
          <div class="form-group">
            <label>Secret value</label>
            <input type="password" id="edit-credential-value" autocomplete="new-password" placeholder="Leave blank to keep current value">
            <p class="form-hint">Leave it empty to keep the saved value.</p>
            <div id="edit-credential-reveal-group">
              <button type="button" class="btn btn-sm btn-secondary" id="edit-credential-reveal">Show value</button>
              <div id="edit-credential-revealed" hidden style="margin-top:8px">
                <code id="edit-credential-revealed-value" style="display:block;white-space:pre-wrap;word-break:break-all;user-select:all;padding:8px;border:1px solid var(--border,#ccc);border-radius:6px"></code>
                <button type="button" class="btn btn-sm btn-secondary" id="edit-credential-reveal-hide">Hide</button>
                <button type="button" class="btn btn-sm btn-secondary" id="edit-credential-reveal-copy">Copy</button>
                <span class="form-hint" id="edit-credential-reveal-note">Hidden again after 30 seconds. Showing it is recorded in the audit log.</span>
              </div>
            </div>
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

    extra_js = '<script src="/static/js/credentials.js?v=20261118n"></script>'

    return render_page("Credentials", body, "/credentials", extra_js, session=session)
