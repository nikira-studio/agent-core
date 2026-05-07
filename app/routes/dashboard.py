from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
import httpx
import json

from app.services.auth_service import validate_session, get_user_by_id, count_users
from app.security.dependencies import get_current_session, require_admin
from app.security.context import build_user_context
from app.security.response_helpers import success_response, error_response
from app.security.scope_enforcer import ScopeEnforcer
from app.config import settings


router = APIRouter()


def _hf(s):
    return s.replace("escape_html", "escapeHtml")


def escape_html(s):
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def get_session_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    token = request.cookies.get("session_token")
    if token:
        return token
    return ""


def require_auth(request: Request):
    token = get_session_token(request)
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    session = validate_session(token, inactivity_minutes=settings.INACTIVITY_TIMEOUT_MINUTES)
    if not session:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return session


def render_page(title: str, body: str, nav_active: str = "", extra_js: str = "", session: dict | None = None, status_code: int = 200) -> HTMLResponse:
    is_admin = bool(session and session.get("role") == "admin")
    nav_items = [
        ("/", "Overview"),
        ("/users", "Users"),
        ("/agents", "Agents"),
        ("/workspaces", "Workspaces"),
        ("/memory", "Memory"),
        ("/vault", "Vault"),
        ("/agent-setup", "Integration"),
        ("/activity", "Activity"),
        ("/settings", "Settings"),
    ]
    if not is_admin:
        nav_items = [(href, label) for href, label in nav_items if href != "/users"]
    nav_html = "\n".join(
        f'<a href="{href}" class="{"active" if nav_active == href else ""}"><span>{label}</span></a>'
        for href, label in nav_items
    )
    return HTMLResponse(f"""<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title} - Agent Core</title>
    <link rel="icon" href="/static/img/favicon/favicon.ico" sizes="any">
    <link rel="icon" type="image/png" sizes="32x32" href="/static/img/favicon/favicon-32x32.png">
    <link rel="icon" type="image/png" sizes="16x16" href="/static/img/favicon/favicon-16x16.png">
    <link rel="apple-touch-icon" sizes="180x180" href="/static/img/favicon/apple-touch-icon.png">
    <link rel="manifest" href="/static/img/favicon/site.webmanifest">
    <link rel="stylesheet" href="/static/css/dashboard.css?v=20260506">
</head>
<body>
<div class="layout">
  <div class="sidebar">
    <a href="/" class="brand-link" aria-label="Agent Core overview">
      <img src="/static/img/logo.png" alt="" class="brand-logo">
      <span>Agent Core</span>
    </a>
    <nav>
      {nav_html}
    </nav>
    <div class="sidebar-footer">
      <button class="theme-toggle" aria-label="Toggle theme"></button>
      <a href="/logout">Logout</a>
    </div>
  </div>
  <div class="main">
    {body}
  </div>
</div>
<script src="/static/js/dashboard.js?v=20260506b"></script>
{extra_js}
</body>
</html>""", status_code=status_code)


def api_key_modal(id: str, title: str, body_content: str) -> str:
    return f"""
<div class="modal-overlay" id="{id}" style="display:none">
  <div class="modal">
    <h3>{title}</h3>
    {body_content}
  </div>
</div>"""


@router.get("/")
async def dashboard_home(request: Request, session: dict = Depends(require_auth)):
    user = get_user_by_id(session["user_id"])
    from app.services import activity_service
    from app.services.agent_service import list_agents
    from app.services.workspace_service import list_workspaces
    from app.database import get_db

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"
    user_scope = f"user:{session['user_id']}"

    def count_rows(table: str, where: str = "1=1", params: tuple = ()) -> int:
        with get_db() as conn:
            row = conn.execute(f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", params).fetchone()
            return int(row["count"] if row else 0)

    if is_admin:
        agents = list_agents()
        workspaces = list_workspaces()
        agent_count = len([a for a in agents if a.get("is_active")])
        workspace_count = len([p for p in workspaces if p.get("is_active")])
        memory_count = count_rows("memory_records", "record_status = 'active'")
        vault_count = count_rows("vault_entries")
        recent_activity = activity_service.list_activities(limit=8)
        attention = activity_service.list_activities(status="stale", limit=8) + activity_service.list_activities(status="blocked", limit=8)
    else:
        agents = list_agents(owner_user_id=session["user_id"])
        workspaces = list_workspaces(owner_user_id=session["user_id"])
        agent_count = len([a for a in agents if a.get("is_active")])
        workspace_count = len([p for p in workspaces if p.get("is_active")])
        memory_count = count_rows("memory_records", "scope = ? AND record_status = 'active'", (user_scope,))
        vault_count = count_rows("vault_entries", "scope = ?", (user_scope,))
        recent_activity = activity_service.list_activities(user_id=session["user_id"], limit=8)
        attention = (
            activity_service.list_activities(user_id=session["user_id"], status="stale", limit=8)
            + activity_service.list_activities(user_id=session["user_id"], status="blocked", limit=8)
        )

    active_task_count = count_rows(
        "agent_activity",
        "status IN ('active', 'reassigned')" if is_admin else "user_id = ? AND status IN ('active', 'reassigned')",
        () if is_admin else (session["user_id"],),
    )
    attention = attention[:8]
    users_card = ""
    if is_admin:
        user_count = count_rows("users")
        users_card = f'<a class="stat-card stat-link" href="/users"><div class="value">{user_count}</div><div class="label">Users</div></a>'

    stat_cards = f"""
    <div class="stat-grid">
      {users_card}
      <a class="stat-card stat-link" href="/agents"><div class="value">{agent_count}</div><div class="label">Active Agents</div></a>
      <a class="stat-card stat-link" href="/workspaces"><div class="value">{workspace_count}</div><div class="label">Active Workspaces</div></a>
      <a class="stat-card stat-link" href="/activity"><div class="value">{active_task_count}</div><div class="label">Open Activities</div></a>
      <a class="stat-card stat-link" href="/activity"><div class="value">{len(attention)}</div><div class="label">Stale / Blocked</div></a>
      <a class="stat-card stat-link" href="/memory"><div class="value">{memory_count}</div><div class="label">Memory Records</div></a>
      <a class="stat-card stat-link" href="/vault"><div class="value">{vault_count}</div><div class="label">Credentials</div></a>
    </div>"""

    attention_html = ""
    if attention:
        attention_rows = "".join(
            f"<tr><td>{a.get('task_description','')[:60]}</td>"
            f"<td><span class='badge badge-{a.get('status','stale')}'>{a.get('status','')}</span></td>"
            f"<td>{a.get('assigned_agent_id','')}</td>"
            f"<td><a href='/activity'>Open</a></td></tr>"
            for a in attention
        )
        attention_html = f"""
    <div class="card attention-card">
      <h3>Needs Attention</h3>
      <table><thead><tr><th>Task</th><th>Status</th><th>Agent</th><th></th></tr></thead><tbody>{attention_rows}</tbody></table>
    </div>"""

    activity_rows = "".join(
        f"<tr><td>{a.get('task_description','')[:50]}</td>"
        f"<td><span class='badge badge-{a.get('status','active')}'>{a.get('status','')}</span></td>"
        f"<td>{a.get('assigned_agent_id','')}</td>"
        f"<td>{str(a.get('updated_at','') or a.get('started_at',''))[:16]}</td></tr>"
        for a in recent_activity[:6]
    ) if recent_activity else "<tr><td colspan=4 class=empty>No recent activity.</td></tr>"

    audit_link = '<a href="/audit" class="btn btn-sm btn-secondary">Audit Log</a>' if is_admin else ""

    return render_page("Overview", f"""
    <div class="page-header"><h1>Overview</h1></div>
    {stat_cards}
    {attention_html}
    <div class="card">
      <div class="section-header">
        <h3>Recent Activity</h3>
        <div class="section-actions">
          <a href="/activity" class="btn btn-sm btn-secondary">View Activity</a>
          {audit_link}
        </div>
      </div>
      <table><thead><tr><th>Task</th><th>Status</th><th>Agent</th><th>Updated</th></tr></thead><tbody>{activity_rows}</tbody></table>
    </div>
    """, "/", session=session)


@router.get("/login")
async def login_page(request: Request):
    user_count = count_users()
    if user_count == 0:
        return render_page("Setup", """
    <div class="card">
      <h3>Welcome to Agent Core</h3>
      <p class="text-muted" style="margin-bottom:20px">Create your admin account to get started.</p>
      <form id="setup-form" onsubmit="submitSetup(event)">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" required>
        </div>
        <div class="form-group">
          <label>Display Name</label>
          <input type="text" name="display_name" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" minlength="8" required>
        </div>
        <button type="submit" class="btn">Create Account</button>
      </form>
    </div>
    <script>
    async function submitSetup(e) {
      e.preventDefault();
      const fd = new FormData(e.target);
      const r = await fetch('/api/auth/register', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fd))});
      const j = await r.json();
      if (j.ok) { showToast('Account created. Set up two-factor authentication next.', 'success'); window.location.href = '/settings/otp?first_run=1'; }
      else { showToast(j.error.message || 'Error', 'danger'); }
    }
    </script>
    """, "")
    return render_page("Login", """
    <div class="card" style="max-width:400px;margin:60px auto">
      <h3>Sign In</h3>
      <form id="login-form" onsubmit="submitLogin(event)">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" required>
        </div>
        <button type="submit" class="btn">Login</button>
      </form>
    </div>
    <script>
    async function submitLogin(e) {
      e.preventDefault();
      const fd = new FormData(e.target);
      const r = await fetch('/api/auth/login', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fd))});
      const j = await r.json();
      if (j.ok) {
        if (j.data.requires_otp) {
          sessionStorage.setItem('temp_session_id', j.data.session_id);
          window.location.href = '/otp';
        } else {
          window.location.href = '/';
        }
      } else { showToast(j.error.message || 'Login failed', 'danger'); }
    }
    </script>
    """, "")


@router.get("/otp")
async def otp_page(request: Request):
    return render_page("OTP Verification", """
    <div class="card" style="max-width:400px;margin:60px auto">
      <h3>Two-Factor Authentication</h3>
      <p class="text-muted" style="margin-bottom:16px">Enter the 6-digit code from your authenticator app, or paste one backup code.</p>
      <form id="otp-form" onsubmit="submitOtp(event)">
        <div class="form-group">
          <input type="text" name="otp_code" placeholder="123456 or backup code" autocomplete="one-time-code" style="width:260px;font-size:1rem;text-align:center">
          <p class="form-hint">Backup codes are single-use and can also be used for OTP-gated backup and vault actions.</p>
        </div>
        <button type="submit" class="btn">Verify</button>
      </form>
    </div>
    <script>
    async function submitOtp(e) {
      e.preventDefault();
      const fd = new FormData(e.target);
      fd.set('session_id', sessionStorage.getItem('temp_session_id') || '');
      const r = await fetch('/api/auth/otp/verify', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(Object.fromEntries(fd))});
      const j = await r.json();
      if (j.ok) { sessionStorage.removeItem('temp_session_id'); window.location.href = '/'; }
      else { showToast(j.error.message || 'Invalid code', 'danger'); }
    }
    </script>
    """, "")


@router.get("/logout")
async def logout_page(request: Request):
    return HTMLResponse("""<html><body>
<script>fetch('/api/auth/logout',{method:'POST'}).finally(()=>{window.location.href='/login'});</script>
<p>Logging out...</p></body></html>""")


# ─── AGENTS ──────────────────────────────────────────────────────────────────

@router.get("/agents")
async def agents_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import agent_service, workspace_service, audit_service
    from app.database import get_db
    is_admin = session.get("role") == "admin"
    agents = agent_service.list_agents() if is_admin else agent_service.list_agents(owner_user_id=session["user_id"])
    all_workspaces = workspace_service.list_workspaces() if is_admin else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    
    def build_scope_list(prefix):
        h = f'<div class="scope-selectors" id="{prefix}-scopes">'
        h += f'<label class="checkbox-label" data-scope-row="shared"><input type="checkbox" data-scope="shared"> <span>Shared workspace</span></label>'
        if all_workspaces:
            h += '<h4>Workspaces</h4>'
            for p in all_workspaces:
                h += f'<label class="checkbox-label" data-scope-row="workspace:{p["id"]}"><input type="checkbox" data-scope="workspace:{p["id"]}"> <span>Workspace: {p["name"]}</span></label>'
        if agents:
            h += '<h4>Other Agent Private Workspaces</h4>'
            for a in agents:
                h += f'<label class="checkbox-label" data-scope-row="agent:{a["id"]}"><input type="checkbox" data-scope="agent:{a["id"]}"> <span>{a["display_name"]} ({a["id"]})</span></label>'
        h += '</div>'
        return h

    ca_read_html = build_scope_list("ca-read")
    ca_write_html = build_scope_list("ca-write")
    edit_read_html = build_scope_list("edit-read")
    edit_write_html = build_scope_list("edit-write")

    def agent_row(a):
        active = a.get('is_active')
        status_badge = f"<span class='badge badge-{'active' if active else 'inactive'}'>{'active' if active else 'inactive'}</span>"
        read_scopes = agent_service.parse_scopes(a.get("read_scopes_json", "[]"))
        write_scopes = agent_service.parse_scopes(a.get("write_scopes_json", "[]"))
        own_scope = f"agent:{a['id']}"
        read_extra = [s for s in read_scopes if s != own_scope]
        write_extra = [s for s in write_scopes if s != own_scope]
        access_summary = (
            f"<span class='scope-tag' title='Implicit private scope'>{own_scope}</span>"
            f"<span class='text-muted'> + {len(read_extra)} read / {len(write_extra)} write grants</span>"
        )
        if active:
            toggle_btn = f"<button type='button' class='btn btn-sm btn-warning' onclick=\"deactivateAgent('{a['id']}')\">Deactivate</button>"
        else:
            toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"reactivateAgent('{a['id']}')\">Reactivate</button>"
        owner_id = a.get('owner_user_id','')
        default_user_id = a.get('default_user_id','') or owner_id
        return (
            f"<tr>"
            f"<td><strong>{a.get('display_name','')}</strong><br><code>{a['id']}</code>"
            f"<div class='text-muted'>Owner: {owner_id} · Default user: {default_user_id}</div></td>"
            f"<td>{status_badge}</td>"
            f"<td>{access_summary}</td>"
            f"<td>{a.get('created_at','')[:10]}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"editAgent('{a['id']}')\">Edit</button>"
            f"<a class='btn btn-sm btn-secondary' href='/agent-setup?agent_id={a['id']}'>Integration</a>"
            f"{toggle_btn}"
            f"<button type='button' class='btn btn-sm btn-danger' onclick=\"purgeAgent('{a['id']}')\" title='Permanently delete'>&#128465;</button>"
            f"</div></td></tr>"
        )

    rows = "".join(agent_row(a) for a in agents)

    js = """
    <script>
    const IS_ADMIN = __IS_ADMIN__;
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
    async function editAgent(id) {
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
      // Hide and disable the agent's own scope row: self-access is implicit.
      ['edit-read-scopes', 'edit-write-scopes'].forEach(containerId => {
        document.querySelectorAll('#' + containerId + ' input').forEach(input => {
          const isOwnScope = input.dataset.scope === 'agent:' + a.id;
          input.disabled = isOwnScope || input.dataset.requiredScope === 'true';
          const label = input.closest('label');
          if (label) {
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
      function normalizeAgentId(value) {
        return (value || '').trim().toLowerCase();
      }

      async function submitEditAgent(e) {
        e.preventDefault();
        const id = document.getElementById('edit-agent-id').textContent;
        const ownScope = 'agent:' + id;
        const body = {
          display_name: document.getElementById('edit-display-name').value,
          description: document.getElementById('edit-description').value,
          read_scopes: getSelectedScopes('edit-read-scopes'),
          write_scopes: getSelectedScopes('edit-write-scopes'),
        };
        body.read_scopes.push(ownScope);
        body.write_scopes.push(ownScope);
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
        if (!body.read_scopes.includes(privateScope)) body.read_scopes.push(privateScope);
        if (!body.write_scopes.includes(privateScope)) body.write_scopes.push(privateScope);

        try {
          const j = await apiFetch('/api/agents', { method: 'POST', body: JSON.stringify(body) });
          if (j.ok) {
            showToast('Agent created');
            closeModal('create-agent-modal');
            document.getElementById('rotate-modal-body').innerHTML =
              '<div class="alert alert-success">Agent created successfully.</div>' +
              '<p class="text-muted">Generate a one-time connection key and tool config from Integrations when you are ready to connect this agent.</p>' +
              '<a class="btn" href="/agent-setup?agent_id=' + encodeURIComponent(j.data.agent.id) + '">Go to Integrations</a>';
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

    return render_page("Agents", f"""
    <div class="page-header"><h1>Agents</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-agent-modal')">+ Create Agent</button>
    </div></div>
    <div class="card">
      <h3>Agent Access</h3>
      <p class="text-muted access-summary">Agents belong to one owner/default user. Use workspaces as shared collaboration spaces; personal user scopes stay tied to the agent owner.</p>
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
            <input type="text" id="ca-id" pattern="[a-z0-9_-]+" placeholder="e.g. coding-agent" required>
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
            <p class="form-hint">Leave blank unless this agent needs workspace, shared, or agent-private context outside its own private workspace. Personal user scope access is limited to the owner/default user.</p>
          </div>
          <div class="form-group">
            <label>Can Write To</label>
            {ca_write_html}
            <p class="form-hint">Grant write access only where the agent should be allowed to save new memory or credentials. Use workspace scopes for multi-user collaboration.</p>
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
        <h3>Edit Agent: <span id="edit-agent-id"></span></h3>
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
            <p class="form-hint">The agent's private workspace is automatic and hidden here. Checked items are extra places this agent can retrieve from.</p>
          </div>
          <div class="form-group">
            <label>Can Write To</label>
            {edit_write_html}
            <p class="form-hint">Checked items are extra places this agent can save to.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-agent-modal')">Cancel</button>
            <button type="submit" class="btn">Save Changes</button>
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
    """, "/agents", js, session=session)


# ─── PROJECTS ────────────────────────────────────────────────────────────────

@router.get("/workspaces")
async def workspaces_page(request: Request, session: dict = Depends(require_auth)):
    import json
    from app.services import workspace_service, agent_service
    workspaces = workspace_service.list_workspaces() if session.get("role") == "admin" else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    all_agents = agent_service.list_agents() if session.get("role") == "admin" else agent_service.list_agents(owner_user_id=session["user_id"])

    project_agent_access = {}
    for p in workspaces:
        pscope = f"workspace:{p['id']}"
        read_agents = [a['id'] for a in all_agents if pscope in (json.loads(a.get('read_scopes_json','[]')) or [])]
        write_agents = [a['id'] for a in all_agents if pscope in (json.loads(a.get('write_scopes_json','[]')) or [])]
        project_agent_access[p['id']] = (read_agents, write_agents)

    def workspace_row(p):
        read_tags = "".join(f"<span class='scope-tag' title='Read'>{a}</span>" for a in project_agent_access[p['id']][0])
        write_tags = "".join(f"<span class='scope-tag scope-write' title='Write'>{a}</span>" for a in project_agent_access[p['id']][1])
        read_tags = read_tags or "<span class='text-muted'>none</span>"
        write_tags = write_tags or "<span class='text-muted'>none</span>"
        is_active = p.get('is_active')
        active_label = 'active' if is_active else 'inactive'
        if is_active:
            toggle_btn = f"<button class='btn btn-sm btn-warning' onclick=\"deactivateProject('{p['id']}')\">Deactivate</button>"
        else:
            toggle_btn = f"<button class='btn btn-sm btn-secondary' onclick=\"reactivateProject('{p['id']}')\">Reactivate</button>"
        return (
            f"<tr>"
            f"<td><code>workspace:{p['id']}</code></td>"
            f"<td>{p.get('name','')}</td>"
            f"<td><span class='badge badge-{active_label}'>{active_label}</span></td>"
            f"<td>{p.get('owner_user_id','')}</td>"
            f"<td class='agent-access-cell'>"
            f"<div class='agent-read-list'><span class='access-label'>Read</span>{read_tags}</div>"
            f"<div class='agent-write-list'><span class='access-label'>Write</span>{write_tags}</div>"
            f"</td>"
            f"<td>{p.get('created_at','')[:10]}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"editProject('{p['id']}')\">Edit</button>"
            f"{toggle_btn}"
            f"<button type='button' class='btn btn-sm btn-danger' onclick=\"purgeProject('{p['id']}')\" title='Permanently delete'>Delete</button>"
            f"</div></td></tr>"
        )

    rows = "".join(workspace_row(p) for p in workspaces)

    agent_options = "".join(f"<option value=\"{a['id']}\">{a['id']}</option>" for a in all_agents)

    js = """
    <script>
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
      const r = await fetch('/api/workspaces/' + id);
      const j = await r.json();
      if (!j.ok) { showToast(j.error.message || 'Error', 'danger'); return; }
      const p = j.data.workspace;
      document.getElementById('ep-id').textContent = p.id;
      document.getElementById('ep-name').value = p.name || '';
      document.getElementById('ep-description').value = p.description || '';
      openModal('edit-workspace-modal');
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
    </script>"""

    return render_page("Workspaces", f"""
    <div class="page-header"><h1>Workspaces</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-workspace-modal')">+ Create Workspace</button>
    </div></div>
    <div class="card">
      <h3>Workspaces</h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Workspaces are shared workspace contexts. Grant agent read or write access from Agents -> Edit. Scope names still use <code>workspace:&lt;id&gt;</code>.</p>
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
            <input type="text" id="cp-id" pattern="[a-z0-9_-]+" placeholder="e.g. myproject" required>
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
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-workspace-modal')">Cancel</button>
            <button type="submit" class="btn">Save</button>
          </div>
        </form>
      </div>
    </div>
    """ + """
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
    </script>""", "/workspaces", js, session=session)


# ─── USERS ───────────────────────────────────────────────────────────────────

@router.get("/users")
async def users_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import list_users
    if session.get("role") != "admin":
        return render_page("Admin Required", """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">Users are managed by administrators.</p>
      <a href="/" class="btn btn-secondary">Back to Overview</a>
    </div>
    """, "/", session=session, status_code=403)

    users = list_users()
    current_user_id = session["user_id"]

    def user_row(u):
        otp = "<span class='badge badge-active'>enrolled</span>" if u.get("otp_enrolled") else "<span class='badge badge-inactive'>none</span>"
        is_self = u["id"] == current_user_id
        user_payload = escape_html(json.dumps({
            "id": u["id"],
            "email": u.get("email", ""),
            "display_name": u.get("display_name", ""),
            "role": u.get("role", "user"),
        }))
        delete_action = (
            "<span class='text-muted' style='font-size:0.8rem'>current session</span>"
            if is_self else
            f"<button type='button' class='btn btn-sm btn-danger' onclick=\"deleteUser('{u['id']}', '{escape_html(u['display_name'])}')\">Delete</button>"
        )
        actions = f"<div class='actions-cell'><button type='button' class='btn btn-sm btn-secondary' data-user='{user_payload}' onclick=\"editUser(this)\">Edit</button>{delete_action}</div>"
        return (
            f"<tr>"
            f"<td>{escape_html(u.get('display_name',''))}</td>"
            f"<td><code>{u['id']}</code></td>"
            f"<td>{escape_html(u.get('email',''))}</td>"
            f"<td><span class='badge badge-{'active' if u.get('role') == 'admin' else 'inactive'}'>{u.get('role','user')}</span></td>"
            f"<td>{otp}</td>"
            f"<td>{u.get('created_at','')[:10]}</td>"
            f"<td>{actions}</td>"
            f"</tr>"
        )

    rows = "".join(user_row(u) for u in users) or "<tr><td colspan=7 class=empty>No users.</td></tr>"

    js = """
    <script>
    async function createUser(e) {
      e.preventDefault();
      const body = Object.fromEntries(new FormData(e.target));
      const j = await apiFetch('/api/auth/users', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) {
        showToast('User created');
        closeModal('create-user-modal');
        location.reload();
      } else { showToast(j.error?.message || 'Failed', 'danger'); }
    }
    function editUser(btn) {
      const u = JSON.parse(btn.getAttribute('data-user'));
      document.getElementById('eu-id').value = u.id;
      document.getElementById('eu-display-name').value = u.display_name || '';
      document.getElementById('eu-email').value = u.email || '';
      document.getElementById('eu-role').value = u.role || 'user';
      document.getElementById('eu-password').value = '';
      openModal('edit-user-modal');
    }
    async function submitEditUser(e) {
      e.preventDefault();
      const id = document.getElementById('eu-id').value;
      const body = {
        display_name: document.getElementById('eu-display-name').value,
        email: document.getElementById('eu-email').value,
        role: document.getElementById('eu-role').value,
      };
      const password = document.getElementById('eu-password').value;
      if (password) body.password = password;
      const j = await apiFetch('/api/auth/users/' + id, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) {
        showToast('User updated');
        closeModal('edit-user-modal');
        location.reload();
      } else { showToast(j.error?.message || 'Failed', 'danger'); }
    }
    async function deleteUser(id, name) {
      if (!confirm('Delete user "' + name + '"? This cannot be undone.')) return;
      const j = await apiFetch('/api/auth/users/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('User deleted'); location.reload(); }
      else { showToast(j.error?.message || 'Failed', 'danger'); }
    }
    </script>"""

    return render_page("Users", f"""
    <div class="page-header"><h1>Users</h1><div class="page-actions">
      <button class="btn" onclick="openModal('create-user-modal')">+ Add User</button>
    </div></div>
    <div class="card">
      <h3>All Users</h3>
      <p class="text-muted" style="margin-bottom:12px">Admin-only view. First-run registration creates the initial admin; after that, admins create users here and assign roles.</p>
      <table><thead><tr><th>Name</th><th>ID</th><th>Email</th><th>Role</th><th>OTP</th><th>Created</th><th class="actions-cell">Actions</th></tr></thead>
      <tbody>{rows}</tbody></table>
    </div>
    <div class="modal-overlay" id="create-user-modal" style="display:none">
      <div class="modal">
        <h3>Add User</h3>
        <form id="create-user-form" onsubmit="createUser(event)">
          <div class="form-group"><label>Display Name</label><input type="text" name="display_name" required></div>
          <div class="form-group"><label>Email</label><input type="email" name="email" required></div>
          <div class="form-row">
            <div class="form-group"><label>Role</label><select name="role"><option value="user">User</option><option value="admin">Admin</option></select></div>
            <div class="form-group"><label>Temporary Password</label><input type="password" name="password" minlength="8" required></div>
          </div>
          <p class="form-hint">Users can change their password after signing in. Admin role grants full dashboard access.</p>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-user-modal')">Cancel</button>
            <button type="submit" class="btn">Create</button>
          </div>
        </form>
      </div>
    </div>
    <div class="modal-overlay" id="edit-user-modal" style="display:none">
      <div class="modal">
        <h3>Edit User</h3>
        <form id="edit-user-form" onsubmit="submitEditUser(event)">
          <input type="hidden" id="eu-id">
          <div class="form-group"><label>Display Name</label><input type="text" id="eu-display-name" required></div>
          <div class="form-group"><label>Email</label><input type="email" id="eu-email" required></div>
          <div class="form-row">
            <div class="form-group"><label>Role</label><select id="eu-role"><option value="user">User</option><option value="admin">Admin</option></select></div>
            <div class="form-group"><label>New Password</label><input type="password" id="eu-password" minlength="8" placeholder="Leave unchanged"></div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-user-modal')">Cancel</button>
            <button type="submit" class="btn">Save</button>
          </div>
        </form>
      </div>
    </div>
    """, "/users", js, session=session)


# ─── VAULT ───────────────────────────────────────────────────────────────────

@router.get("/vault")
async def vault_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import vault_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service

    ctx = build_user_context(session)
    enforcer = ScopeEnforcer(ctx.read_scopes, ctx.write_scopes, ctx.agent_id, is_admin=ctx.is_admin, active_workspace_ids=ctx.active_workspace_ids)
    entries = [
        e for e in (vault_service.list_vault_entries(limit=500) or [])
        if enforcer.can_read(e.get("scope", ""))
    ]
    agents = list_agents() if session.get("role") == "admin" else list_agents(owner_user_id=session["user_id"])
    workspaces = workspace_service.list_workspaces() if session.get("role") == "admin" else workspace_service.list_workspaces(owner_user_id=session["user_id"])

    agent_options = "".join(
        f"<option value=\"agent:{a['id']}\">Agent: {escape_html(a.get('display_name') or a['id'])} (agent:{a['id']})</option>"
        for a in agents
    )
    project_options = "".join(
        f"<option value=\"workspace:{p['id']}\">Workspace: {escape_html(p.get('name') or p['id'])} (workspace:{p['id']})</option>"
        for p in workspaces
    )
    user_scope = f"user:{session['user_id']}"
    all_scopes = sorted(set([e.get("scope","") for e in entries] + [user_scope, "shared"]))
    category_options = [
        ("api", "API Key / Token"),
        ("password", "Password"),
        ("url", "URL / Endpoint"),
        ("config", "Config / Text"),
        ("other", "Other"),
    ]
    category_labels = dict(category_options)
    category_filter_options = "".join(
        f"<option value=\"{value}\">{label}</option>"
        for value, label in category_options
    )
    category_select_options = "".join(
        f"<option value=\"{value}\">{label}</option>"
        for value, label in category_options
    )

    scope_groups = {}
    for e in entries:
        scope = e.get("scope","")
        if scope not in scope_groups:
            scope_groups[scope] = []
        scope_groups[scope].append(e)

    groups_html = ""
    for scope in sorted(scope_groups.keys()):
        group_entries = scope_groups[scope]
        rows = "".join(
            f"<tr data-category=\"{escape_html(e.get('value_type','other') or 'other')}\">"
            f"<td>{e.get('name','')}</td>"
            f"<td><code>{e.get('reference_name','')}</code> "
            f"<button class='copy-btn' onclick=\"copyRef('{e['reference_name']}', this)\">Copy</button></td>"
            f"<td>{escape_html(category_labels.get(e.get('value_type',''), e.get('value_type','') or 'Other'))}</td>"
            f"<td>{e.get('expires_at','')[:10] if e.get('expires_at') else '-'}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"editVault('{e['id']}')\">Edit</button>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"revealVault('{e['id']}')\">Reveal</button>"
            f"<button type='button' class='btn btn-sm btn-danger' onclick=\"deleteVault('{e['id']}')\">Delete</button>"
            f"</div></td></tr>"
            for e in group_entries
        )
        groups_html += f"""
        <div class="vault-scope-group">
        <div class="section-title">{scope}</div>
	      <table><thead><tr><th>Name</th><th>Reference</th><th>Category</th><th>Expires</th><th class="actions-cell">Actions</th></tr></thead>
        <tbody>{rows}</tbody></table>
        </div>"""

    js = """
    <script>
    async function refreshVault() { location.reload(); }
    async function deleteVault(id) {
      if (!confirm('Delete this credential? This cannot be undone.')) return;
      const j = await apiFetch('/api/vault/entries/' + id, { method: 'DELETE' });
      if (j.ok) { 
        showToast('Deleted', 'success'); 
        refreshVault(); 
      } else { 
        showToast(j.error?.message || 'Failed to delete credential', 'danger'); 
      }
    }
    async function editVault(id) {
      const j = await apiFetch('/api/vault/entries/' + id);
      if (!j.ok) { showToast(j.error.message || 'Error', 'danger'); return; }
      const e = j.data.entry;
      document.getElementById('ve-id').value = id;
      document.getElementById('ve-scope').textContent = e.scope || '';
      document.getElementById('ve-ref').textContent = e.reference_name || '';
      document.getElementById('ve-created-by').textContent = e.created_by || '-';
      document.getElementById('ve-name').value = e.name || '';
      document.getElementById('ve-label').value = e.label || '';
      document.getElementById('ve-value-type').value = e.value_type || 'other';
      document.getElementById('ve-expires-at').value = e.expires_at ? e.expires_at.substring(0, 16) : '';
      document.getElementById('ve-value').value = '';
      openModal('edit-vault-modal');
    }
    async function submitEditVault(e) {
      e.preventDefault();
      const id = document.getElementById('ve-id').value;
      const body = {
        name: document.getElementById('ve-name').value,
        label: document.getElementById('ve-label').value || null,
        value_type: document.getElementById('ve-value-type').value || null,
        expires_at: document.getElementById('ve-expires-at').value || null,
      };
      const value = document.getElementById('ve-value').value;
      if (value) body.value = value;
      const j = await apiFetch('/api/vault/entries/' + id, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) { showToast('Updated'); closeModal('edit-vault-modal'); refreshVault(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function revealVault(id) {
      const code = prompt('Enter OTP or backup code to reveal credential:');
      if (!code) return;
      const j = await apiFetch('/api/vault/entries/' + id + '/reveal', {
        method: 'POST', body: JSON.stringify({ otp_code: code })
      });
      if (j.ok) {
        const value = j.data.value || '';
        document.getElementById('reveal-value').innerHTML =
          '<div class="api-key-display"><code>' + escapeHtml(value) + '</code></div>' +
          '<button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(value) + ', this)">Copy value</button>';
        openModal('reveal-vault-modal');
      } else { showToast(j.error.message || 'Failed (check OTP)', 'danger'); }
    }
    function copyRef(ref, btn) {
      copyToClipboard(ref, btn);
    }
    function filterVaultByCategory() {
      const selected = document.getElementById('vault-category-filter').value;
      document.querySelectorAll('#vault-entries tbody tr').forEach(row => {
        row.style.display = (!selected || row.dataset.category === selected) ? '' : 'none';
      });
      document.querySelectorAll('.vault-scope-group').forEach(group => {
        const visibleRows = Array.from(group.querySelectorAll('tbody tr')).some(row => row.style.display !== 'none');
        group.style.display = visibleRows ? '' : 'none';
      });
    }
    function escapeHtml(s) {
      return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    async function createVault(e) {
      e.preventDefault();
      const body = {
        scope: document.getElementById('vault-scope').value,
        name: document.getElementById('vault-name').value,
        label: document.getElementById('vault-label').value || '',
        value: document.getElementById('vault-value').value,
        value_type: document.getElementById('vault-value-type').value,
        expires_at: document.getElementById('vault-expires').value || null,
      };
      const j = await apiFetch('/api/vault/entries', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) {
        closeModal('create-vault-modal');
        document.getElementById('create-vault-form').reset();
        const ref = j.data.entry.reference_name || '';
        document.getElementById('vault-created-ref').innerHTML =
          'Credential created. Reference: <code>' + escapeHtml(ref) + '</code> ' +
          '<button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(ref) + ', this)">Copy</button>';
        openModal('vault-created-modal');
      } else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    </script>"""

    return render_page("Vault", f"""
    <div class="page-header"><h1>Vault</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-vault-modal')">+ Add Credential</button>
    </div></div>
    <div id="vault-onpage-ref" style="margin-bottom:16px"></div>
    <div class="card">
      <div class="section-header">
        <h3>Stored Credentials</h3>
        <div class="section-actions">
          <select id="vault-category-filter" onchange="filterVaultByCategory()" aria-label="Filter by category">
            <option value="">All categories</option>
            {category_filter_options}
          </select>
        </div>
      </div>
      <div id="vault-entries">
        {groups_html or '<div class="empty">No credentials stored yet.</div>'}
      </div>
    </div>

    <!-- Create Vault Modal -->
    <div class="modal-overlay" id="create-vault-modal" style="display:none">
      <div class="modal">
        <h3>Add Credential</h3>
        <form id="create-vault-form" onsubmit="createVault(event)">
          <div class="form-group">
	            <label>Scope *</label>
	            <select id="vault-scope" required>
	              <option value="">Select scope...</option>
	              <option value="{user_scope}">Personal user credentials ({user_scope})</option>
	              {project_options}
	              {agent_options}
	              <option value="shared">Shared system credentials (shared)</option>
	            </select>
	            <p class="form-hint">Personal user credentials can be used by agents that have your user scope. Workspace credentials use <code>workspace:&lt;id&gt;</code> scopes and are best for team/workspace context. Agent credentials are private to one agent. Shared is a broad system scope for non-personal credentials and should be used sparingly.</p>
	          </div>
          <div class="form-group">
            <label>Name *</label>
            <input type="text" id="vault-name" placeholder="e.g. github-token" required>
          </div>
          <div class="form-group">
            <label>Label</label>
            <input type="text" id="vault-label" placeholder="e.g. GitHub Personal Access Token">
          </div>
          <div class="form-group">
            <label>Value *</label>
            <textarea id="vault-value" rows="2" required></textarea>
          </div>
          <div class="form-row">
            <div class="form-group">
	              <label>Category</label>
	              <select id="vault-value-type">
	                {category_select_options}
	              </select>
	              <p class="form-hint">Used for sorting and filtering only. Scope controls access.</p>
	            </div>
          </div>
          <div class="form-group">
            <label>Expires At</label>
            <input type="datetime-local" id="vault-expires">
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-vault-modal')">Cancel</button>
            <button type="submit" class="btn">Store</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Edit Vault Modal -->
    <div class="modal-overlay" id="edit-vault-modal" style="display:none">
      <div class="modal">
        <h3>Edit Credential</h3>
        <form id="edit-vault-form" onsubmit="submitEditVault(event)">
          <input type="hidden" id="ve-id">
          <div class="form-row">
            <div class="form-group"><label>Scope</label><code id="ve-scope"></code></div>
            <div class="form-group"><label>Reference</label><code id="ve-ref"></code></div>
          </div>
          <div class="form-group">
            <label>Name</label>
            <input type="text" id="ve-name">
          </div>
          <div class="form-group">
            <label>Label</label>
            <input type="text" id="ve-label">
          </div>
          <div class="form-row">
            <div class="form-group">
	              <label>Category</label>
	              <select id="ve-value-type">
	                {category_select_options}
	              </select>
	            </div>
          </div>
          <div class="form-group">
            <label>Expires At</label>
            <input type="datetime-local" id="ve-expires-at">
          </div>
          <div class="form-group">
            <label>Replace Value</label>
            <textarea id="ve-value" rows="2" placeholder="Leave blank to keep the current encrypted value"></textarea>
            <p class="form-hint">The stored secret is never displayed here. Use Reveal with OTP only when you need to inspect it.</p>
          </div>
          <div class="form-group"><label>Created By</label><span id="ve-created-by" class="text-muted"></span></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-vault-modal')">Cancel</button>
            <button type="submit" class="btn">Save</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Reveal Modal -->
    <div class="modal-overlay" id="reveal-vault-modal" style="display:none">
      <div class="modal">
        <h3>Credential Value</h3>
        <div class="alert alert-warning">This value is sensitive. Do not share it.</div>
        <div id="reveal-value"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('reveal-vault-modal')">Close</button>
        </div>
      </div>
    </div>

    <!-- Created confirmation -->
    <div class="modal-overlay" id="vault-created-modal" style="display:none">
      <div class="modal">
        <h3>Credential Stored</h3>
        <div id="vault-created-ref"></div>
        <p class="text-muted" style="margin-top:8px">The raw value will not be shown again. Use Reveal with OTP if needed.</p>
        <div class="modal-footer">
          <button class="btn" onclick="closeModal('vault-created-modal'); refreshVault();">Done</button>
        </div>
      </div>
    </div>
    """, "/vault", js, session=session)


# ─── MEMORY ──────────────────────────────────────────────────────────────────

@router.get("/memory")
async def memory_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import memory_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service
    from app.database import get_db

    is_admin = session.get("role") == "admin"
    agents = list_agents() if session.get("role") == "admin" else list_agents(owner_user_id=session["user_id"])
    agent_options = "".join(
        f"<option value=\"agent:{a['id']}\">Agent: {escape_html(a.get('display_name') or a['id'])} (agent:{a['id']})</option>"
        for a in agents
    )

    workspaces = workspace_service.list_workspaces() if session.get("role") == "admin" else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    project_options = "".join(
        f"<option value=\"workspace:{p['id']}\">Workspace: {escape_html(p.get('name') or p['id'])} (workspace:{p['id']})</option>"
        for p in workspaces
    )
    user_scope = f"user:{session['user_id']}"
    user_scope_label = f"Personal user memory ({user_scope})"

    visible_scopes = [user_scope] + [f"workspace:{p['id']}" for p in workspaces]

    def list_visible_memory(record_status):
        if is_admin:
            with get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT id, content, memory_class, scope, domain, topic, confidence, importance,
                           source_kind, event_time, created_at, record_status, superseded_by_id, supersedes_id
                    FROM memory_records
                    WHERE record_status = ?
                    ORDER BY created_at DESC
                    LIMIT 200
                    """,
                    (record_status,),
                ).fetchall()
            return [dict(r) for r in rows]

        records = []
        for scope in visible_scopes:
            records.extend(memory_service.get_memory_by_scope(scope=scope, limit=200, record_status=record_status) or [])
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records[:200]

    active_records = list_visible_memory("active")
    retracted_records = list_visible_memory("retracted")

    def active_row(r):
        return (
            f"<tr><td><span class='badge badge-{r.get('memory_class','')}'>{r.get('memory_class','')}</span></td>"
            f"<td>{escape_html(r.get('content','')[:80])}</td>"
            f"<td><code>{r.get('scope','')}</code></td>"
            f"<td>{r.get('domain','') or ''}</td>"
            f"<td>{r.get('confidence',0.5):.1f}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"viewMemory('{r['id']}')\">Detail</button>"
            f"<button type='button' class='btn btn-sm btn-warning' onclick=\"retractRecord('{r['id']}')\">Retract</button>"
            f"<button type='button' class='btn btn-sm btn-danger' onclick=\"deleteRecord('{r['id']}')\" title='Permanently delete'>Delete</button>"
            f"</div></td></tr>"
        )

    def retracted_row(r):
        return (
            f"<tr style='opacity:0.65'><td><span class='badge badge-inactive'>{r.get('memory_class','')}</span></td>"
            f"<td>{escape_html(r.get('content','')[:80])}</td>"
            f"<td><code>{r.get('scope','')}</code></td>"
            f"<td>{r.get('domain','') or ''}</td>"
            f"<td>{r.get('confidence',0.5):.1f}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"restoreRecord('{r['id']}')\">Restore</button>"
            f"<button type='button' class='btn btn-sm btn-danger' onclick=\"deleteRecord('{r['id']}')\" title='Permanently delete'>Delete</button>"
            f"</div></td></tr>"
        )

    records_rows = "".join(active_row(r) for r in active_records) or "<tr><td colspan=6 class=empty>No active records.</td></tr>"
    retracted_rows = "".join(retracted_row(r) for r in retracted_records)

    js = """
    <script>
    async function refreshMemory() { location.reload(); }
    async function retractRecord(id) {
      if (!confirm('Retract this memory record? It will be hidden but can be restored.')) return;
      const j = await apiFetch('/api/memory/retract?record_id=' + id, { method: 'POST' });
      if (j.ok) { showToast('Retracted'); refreshMemory(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function restoreRecord(id) {
      if (!confirm('Restore this memory record?')) return;
      const j = await apiFetch('/api/memory/restore?record_id=' + id, { method: 'POST' });
      if (j.ok) { showToast('Restored'); refreshMemory(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function deleteRecord(id) {
      if (!confirm('PERMANENTLY DELETE this memory record? This cannot be undone.')) return;
      const j = await apiFetch('/api/memory/' + id, { method: 'DELETE' });
      if (j.ok) { 
        showToast('Deleted', 'success'); 
        refreshMemory(); 
      } else { 
        showToast(j.error?.message || 'Failed to delete record', 'danger'); 
      }
    }
    async function viewMemory(id) {
      const j = await apiFetch('/api/memory/' + id);
      if (!j.ok) { showToast(j.error.message || 'Failed', 'danger'); return; }
      const r = j.data.record;
      document.getElementById('mem-detail-content').textContent = r.content || '';
      document.getElementById('mem-detail-class').textContent = r.memory_class || '';
      document.getElementById('mem-detail-scope').textContent = r.scope || '';
      document.getElementById('mem-detail-domain').textContent = r.domain || '';
      document.getElementById('mem-detail-topic').textContent = r.topic || '';
      document.getElementById('mem-detail-confidence').textContent = r.confidence != null ? r.confidence.toFixed(2) : '';
      document.getElementById('mem-detail-importance').textContent = r.importance != null ? r.importance.toFixed(2) : '';
      document.getElementById('mem-detail-created').textContent = r.created_at ? r.created_at.substring(0, 19) : '';
      const supersedeEl = document.getElementById('mem-detail-supersede');
      if (r.superseded_by_id) {
        supersedeEl.textContent = 'Superseded by: ' + r.superseded_by_id.substring(0, 12) + '...';
        supersedeEl.style.display = 'block';
      } else {
        supersedeEl.style.display = 'none';
      }
      document.getElementById('mem-detail-id').value = id;
      openModal('memory-detail-modal');
    }
    async function showChain() {
      const id = document.getElementById('mem-detail-id')?.value || '';
      if (!id) return;
      const j = await apiFetch('/api/memory/' + id + '/chain');
      if (!j.ok) { showToast('Failed to load chain', 'danger'); return; }
      const chain = j.data.chain || [];
      const el = document.getElementById('mem-chain-content');
      if (!chain.length) { el.textContent = 'No supersession chain.'; return; }
      el.innerHTML = chain.map((r, i) => '<div style="margin:4px 0">' + (i > 0 ? '<span class="text-muted">&lt;- </span>' : '') + '<span class="badge badge-' + (r.memory_class || '') + '">' + (r.memory_class || '') + '</span> ' + escapeHtml(r.content || '').substring(0, 60) + ' <span class="text-muted">' + (r.created_at || '').substring(0, 10) + '</span></div>').join('');
      document.getElementById('mem-detail-chain').style.display = 'block';
    }
    async function doSearch() {
      const query = document.getElementById('mem-query').value;
      const scope = document.getElementById('mem-scope').value;
      const memClass = document.getElementById('mem-class').value;
      const domain = document.getElementById('mem-search-domain').value.trim();
      const topic = document.getElementById('mem-search-topic').value.trim();
      const minConfidence = parseFloat(document.getElementById('mem-min-confidence').value);
      const body = { query, limit: 50 };
      if (scope) body.scope = scope;
      if (memClass) body.memory_class = memClass;
      if (domain) body.domain = domain;
      if (topic) body.topic = topic;
      if (!Number.isNaN(minConfidence) && minConfidence > 0) body.min_confidence = minConfidence;
      const j = await apiFetch('/api/memory/search', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) { displayRecords(j.data.records || []); }
      else { showToast(j.error.message || 'Search failed', 'danger'); }
    }
    async function doWrite(e) {
      e.preventDefault();
      const body = {
	        content: document.getElementById('mem-content').value,
	        memory_class: document.getElementById('mem-write-class').value,
	        scope: document.getElementById('mem-write-scope').value || '""" + user_scope + """',
	        domain: document.getElementById('mem-domain').value || null,
	        topic: document.getElementById('mem-topic').value || null,
	        confidence: parseFloat(document.getElementById('mem-confidence').value) || 0.5,
	        importance: parseFloat(document.getElementById('mem-importance').value) || 0.5,
	        source_kind: 'operator_authored',
	      };
      const j = await apiFetch('/api/memory/write', { method: 'POST', body: JSON.stringify(body) });
      if (j.ok) { showToast('Written'); closeModal('write-memory-modal'); refreshMemory(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    function displayRecords(records) {
      const tbody = document.getElementById('mem-results-body');
      if (!records.length) { tbody.innerHTML = '<tr><td colspan=6 class=empty>No records found.</td></tr>'; return; }
      tbody.innerHTML = records.map(r => `
        <tr>
          <td><span class="badge badge-${r.memory_class}">${r.memory_class}</span></td>
          <td>${escapeHtml(r.content || '').substring(0, 80)}</td>
          <td><code>${r.scope || ''}</code></td>
          <td>${r.domain || ''}</td>
          <td>${(r.confidence || 0.5).toFixed(1)}</td>
          <td><button class="btn btn-sm btn-danger" onclick="retractRecord('${r.id}')">Retract</button></td>
        </tr>`).join('');
    }
    function copyGeneratedOutput(btn) {
      copyToClipboard(document.querySelector('#ig-output pre').textContent, btn);
    }

    function escapeHtml(s) {
      return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    function toggleFilters() {
      const f = document.getElementById('filter-panel');
      f.style.display = f.style.display === 'none' ? 'block' : 'none';
    }
    </script>"""

    return render_page("Memory", _hf(f"""
    <div class="page-header"><h1>Memory</h1><div class="page-actions">
        <button class="btn" onclick="openModal('write-memory-modal')">+ Write Memory</button>
        <button class="btn btn-secondary" onclick="toggleFilters()">Search</button>
    </div></div>

    <!-- Search Filters -->
    <div class="card" id="filter-panel" style="display:none">
      <h3>Search Memory</h3>
      <div class="filter-bar">
        <input type="text" id="mem-query" class="search-input" placeholder="Search query...">
        <select id="mem-scope" title="Access scope filter">
          <option value="">All readable scopes</option>
          <option value="{user_scope}">{user_scope_label}</option>
          {agent_options}
          {project_options}
          <option value="shared">Shared memory (shared)</option>
        </select>
	        <select id="mem-class" title="Memory class filter">
	          <option value="">All memory classes</option>
	          <option value="fact">fact</option>
	          <option value="preference">preference</option>
	          <option value="decision">decision</option>
	          <option value="scratchpad">scratchpad</option>
	        </select>
        <input type="text" id="mem-search-domain" placeholder="Domain filter, e.g. engineering">
        <input type="text" id="mem-search-topic" placeholder="Topic filter, e.g. docker">
        <input type="number" id="mem-min-confidence" placeholder="Min confidence" min="0" max="1" step="0.1">
        <button class="btn" onclick="doSearch()">Search</button>
      </div>
      <p class="form-hint">Search uses scope permissions first, then optional class/domain/topic/min-confidence filters. Importance affects ranking when records match.</p>
    </div>

    <!-- Active Records -->
    <div class="card">
      <h3>Active Records <span id="mem-count" class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(active_records)})</span></h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">This list shows active memory visible in the dashboard. Admins see all scopes; non-admin users see personal memory and owned workspace scopes. Use Search to inspect agent, workspace, shared, class, domain, topic, or confidence-filtered records you can read.</p>
      <table><thead><tr><th>Class</th><th>Content</th><th>Scope</th><th>Domain</th><th>Confidence</th><th>Actions</th></tr></thead>
      <tbody id="mem-results-body">
        {records_rows}
      </tbody>
      <input type="hidden" id="current-scope" value="{user_scope}">
    </div>

    <!-- Retracted Records -->
    """ + (f"""
    <div class="card" style="border-left:4px solid var(--text-muted)">
      <h3 style="color:var(--text-muted)">Retracted Records <span class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(retracted_records)})</span></h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">These records are hidden from search. Restore to make them active again, or permanently delete.</p>
      <table><thead><tr><th>Class</th><th>Content</th><th>Scope</th><th>Domain</th><th>Confidence</th><th>Actions</th></tr></thead>
      <tbody>{retracted_rows or "<tr><td colspan=6 class=empty>No retracted records.</td></tr>"}</tbody></table>
    </div>
    """ if retracted_records else "") + f"""

    <!-- Write Memory Modal -->
    <div class="modal-overlay" id="write-memory-modal" style="display:none">
      <div class="modal">
        <h3>Write Memory</h3>
        <form id="write-memory-form" onsubmit="doWrite(event)">
	          <div class="form-group">
	            <label>Content *</label>
	            <textarea id="mem-content" rows="3" required></textarea>
	            <p class="form-hint">Write durable context agents should retrieve later. Agent Core stores exactly what you enter here.</p>
	          </div>
          <div class="form-row">
            <div class="form-group">
	              <label>Class *</label>
	              <select id="mem-write-class" required>
	                <option value="fact">Fact - objective context, used in search and briefings</option>
	                <option value="decision">Decision - chosen direction or rationale, used in handoff briefings</option>
	                <option value="preference">Preference - user/team preference, used in search and briefings</option>
	                <option value="scratchpad">Scratchpad - temporary working note, eligible for pruning</option>
	              </select>
	              <p class="form-hint">Agents must choose this when writing memory. Fact, decision, and preference are used by handoff briefings; scratchpad is temporary and can be pruned by maintenance.</p>
	            </div>
            <div class="form-group">
              <label>Scope *</label>
              <select id="mem-write-scope" required>
                <option value="{user_scope}" selected>{user_scope_label}</option>
                {agent_options}
                {project_options}
	                <option value="shared">Shared memory (shared)</option>
	              </select>
	              <p class="form-hint">This is access control. Personal user memory is <code>{user_scope}</code>; workspace scopes are for team/workspace context; agent scopes are private to one agent; shared is broad and PII-checked.</p>
	            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
	              <label>Domain</label>
	              <input type="text" id="mem-domain" placeholder="e.g. coding">
	              <p class="form-hint">Optional exact-match search filter. Not inferred automatically.</p>
	            </div>
	            <div class="form-group">
	              <label>Topic</label>
	              <input type="text" id="mem-topic" placeholder="e.g. style">
	              <p class="form-hint">Optional exact-match search filter. Not inferred automatically.</p>
	            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
	              <label>Confidence</label>
	              <input type="number" id="mem-confidence" value="1" min="0" max="1" step="0.1">
	              <p class="form-hint">Stored with the record and used by min-confidence search filtering.</p>
	            </div>
	            <div class="form-group">
	              <label>Importance</label>
	              <input type="number" id="mem-importance" value="0.7" min="0" max="1" step="0.1">
	              <p class="form-hint">Used to rank matching search results; higher values surface earlier.</p>
	            </div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('write-memory-modal')">Cancel</button>
            <button type="submit" class="btn">Write</button>
          </div>
        </form>
      </div>
    </div>

    <!-- Memory Detail Modal -->
    <div class="modal-overlay" id="memory-detail-modal" style="display:none">
      <div class="modal" style="max-width:600px">
        <h3>Memory Detail</h3>
        <div class="form-group"><label>Content</label><div id="mem-detail-content" style="background:var(--bg);padding:8px;border-radius:6px;white-space:pre-wrap;max-height:200px;overflow-y:auto"></div></div>
        <div class="form-row">
          <div class="form-group"><label>Class</label><span id="mem-detail-class" class="badge"></span></div>
          <div class="form-group"><label>Scope</label><code id="mem-detail-scope"></code></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Domain</label><span id="mem-detail-domain"></span></div>
          <div class="form-group"><label>Topic</label><span id="mem-detail-topic"></span></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Confidence</label><span id="mem-detail-confidence"></span></div>
          <div class="form-group"><label>Importance</label><span id="mem-detail-importance"></span></div>
        </div>
        <div class="form-group"><label>Created</label><span id="mem-detail-created" class="text-muted"></span></div>
        <input type="hidden" id="mem-detail-id" value="">
        <div id="mem-detail-supersede" class="alert alert-warning" style="display:none"></div>
        <div id="mem-detail-chain" style="display:none">
          <h4 style="margin-top:12px">Supersession Chain</h4>
          <div id="mem-chain-content" class="text-muted" style="font-size:0.85rem"></div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-sm btn-secondary" onclick="showChain()">Show Chain</button>
          <button class="btn btn-secondary" onclick="closeModal('memory-detail-modal')">Close</button>
        </div>
      </div>
    </div>
    """), "/memory", js, session=session)


# ─── ACTIVITY ────────────────────────────────────────────────────────────────

@router.get("/activity")
async def activity_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import activity_service, briefing_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"
    activities = activity_service.list_activities(
        user_id=None if is_admin else session["user_id"],
        limit=100,
    ) or []
    all_agents = list_agents() if is_admin else list_agents(owner_user_id=session["user_id"])
    agent_options = "".join(f"<option value=\"{a['id']}\">{a.get('display_name', a['id'])}</option>" for a in all_agents)
    workspaces = workspace_service.list_workspaces() if is_admin else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    user_scope = f"user:{session['user_id']}"
    activity_scope_options = (
        f"<option value=\"{user_scope}\">Personal user memory ({user_scope})</option>"
        + "".join(
            f"<option value=\"workspace:{p['id']}\">Workspace: {escape_html(p.get('name') or p['id'])} (workspace:{p['id']})</option>"
            for p in workspaces
        )
        + "".join(
            f"<option value=\"agent:{a['id']}\">Agent private: {escape_html(a.get('display_name') or a['id'])} (agent:{a['id']})</option>"
            for a in all_agents
        )
    )
    # For reassign modal, we want the same options
    reassign_options = agent_options

    status_filters = ["active", "stale", "reassigned", "completed", "blocked", "cancelled"]
    status_tabs = "".join(
        f"<button class='btn btn-sm {'btn' if i==0 else 'btn-secondary'} status-filter' data-status='{s}' onclick='filterActivity(\"{s}\",this)'>{s.title()}</button>"
        for i, s in enumerate(status_filters)
    )

    rows = "".join(
        f"<tr class='activity-row' data-status='{a.get('status','')}'>"
        f"<td><code>{a.get('id','')[:12]}</code></td>"
        f"<td>{a.get('task_description','')[:60]}</td>"
        f"<td><span class='badge badge-{a.get('status','active')}'>{a.get('status','')}</span></td>"
        f"<td>{a.get('assigned_agent_id','')}</td>"
        f"<td>{str(a.get('updated_at',''))[:16]}</td>"
        f"<td><div class='actions-cell'>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"createHandoff('{a['id']}')\">Briefing</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"reassignActivity('{a['id']}')\" title='Reassign'>Reassign</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"updateActivity('{a['id']}','active')\" {'disabled' if a.get('status') not in ('stale','blocked','reassigned') else ''} title='Reactivate'>Start</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"updateActivity('{a['id']}','completed')\" {'disabled' if a.get('status') not in ('active','stale','blocked','reassigned') else ''} title='Complete'>Done</button>"
        f"<button type='button' class='btn btn-sm btn-danger' onclick=\"cancelActivity('{a['id']}')\" {'disabled' if a.get('status') not in ('active','stale','blocked','reassigned') else ''} title='Cancel'>Cancel</button>"
        f"</div></td></tr>"
        for a in activities
    )

    js = """
    <script>
    async function refreshActivity() { location.reload(); }
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
    async function createHandoff(id) {
      const j = await apiFetch('/api/briefings/handoff', { method: 'POST', body: JSON.stringify({ activity_id: id }) });
      if (j.ok) {
        const b = j.data.briefing;
        const facts = (b.facts || []).map(f => '<li>' + escapeHtml(f.content || '').substring(0,80) + '</li>').join('');
        const decisions = (b.decisions || []).map(d => '<li>' + escapeHtml(d.content || '').substring(0,80) + '</li>').join('');
        document.getElementById('briefing-content').innerHTML =
          '<h4>Facts</h4><ul>' + (facts || '<li class=text-muted>None</li>') + '</ul>' +
          '<h4 style="margin-top:12px">Decisions</h4><ul>' + (decisions || '<li class=text-muted>None</li>') + '</ul>';
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
    function filterActivity(status, btn) {
      document.querySelectorAll('.status-filter').forEach(b => b.classList.remove('btn'));
      document.querySelectorAll('.status-filter').forEach(b => b.classList.add('btn-secondary'));
      btn.classList.remove('btn-secondary');
      btn.classList.add('btn');
      document.querySelectorAll('.activity-row').forEach(row => {
        row.style.display = (status === 'all' || row.dataset.status === status) ? '' : 'none';
      });
    }
    function copyGeneratedOutput(btn) {
      copyToClipboard(document.querySelector('#ig-output pre').textContent, btn);
    }

    function escapeHtml(s) {
      return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    }
    </script>"""

    return render_page("Activity", f"""
    <div class="page-header"><h1>Activity</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-activity-modal')">+ New Activity</button>
    </div></div>
    <div class="card">
      <h3>Tasks</h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Activities track agent work, stale tasks, reassignment, and handoff briefings. Use workspace memory scope for workspace work and agent private scope for agent-only scratch work.</p>
      <div class="filter-bar" style="margin-bottom:12px">
        <button class='btn btn-sm status-filter' data-status='all' onclick='filterActivity("all",this)'>All</button>
        {status_tabs}
      </div>
      <table><thead><tr><th>ID</th><th>Task</th><th>Status</th><th>Agent</th><th>Updated</th><th class="actions-cell">Actions</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=6 class=empty>No activities yet.</td></tr>"}</tbody></table>
    </div>

    <!-- Create Activity Modal -->
    <div class="modal-overlay" id="create-activity-modal" style="display:none">
      <div class="modal">
        <h3>New Activity</h3>
        <form id="create-activity-form" onsubmit="doCreateActivity(event)">
          <div class="form-group">
            <label>Assigned Agent *</label>
            <select id="act-agent" required>
              <option value="">Select agent...</option>
              {agent_options}
            </select>
          </div>
          <div class="form-group">
            <label>Task Description *</label>
            <textarea id="act-task" rows="3" required></textarea>
          </div>
          <div class="form-group">
            <label>Memory Scope *</label>
            <select id="act-memory-scope" required>
              {activity_scope_options}
            </select>
            <p class="form-hint">Use the full prefixed scope. Workspace activities should usually use the workspace scope, such as <code>workspace:agent-core</code>.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('create-activity-modal')">Cancel</button>
            <button type="submit" class="btn">Create</button>
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

    <!-- Briefing Modal -->
    <div class="modal-overlay" id="briefing-modal" style="display:none">
      <div class="modal" style="max-width:600px">
        <h3>Handoff Briefing</h3>
        <div id="briefing-content" style="max-height:400px;overflow-y:auto"></div>
        <div class="modal-footer">
          <button class="btn btn-secondary" onclick="closeModal('briefing-modal')">Close</button>
        </div>
      </div>
    </div>
    """, "/activity", js, session=session)


# ─── AUDIT ────────────────────────────────────────────────────────────────────

@router.get("/audit")
async def audit_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import audit_service
    if session.get("role") != "admin":
        return render_page("Admin Required", """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">The audit log is available to administrators only.</p>
      <a href="/" class="btn btn-secondary">Back to Overview</a>
    </div>
    """, "/", session=session, status_code=403)

    page = int(request.query_params.get("page", 1))
    limit = 50
    offset = (page - 1) * limit

    actor_filter = request.query_params.get("actor_type", "")
    action_filter = request.query_params.get("action", "")
    resource_filter = request.query_params.get("resource_type", "")
    result_filter = request.query_params.get("result", "")

    from app.services.audit_service import ACTOR_TYPES, RESULT_TYPES, AUDIT_ACTIONS
    all_events = audit_service.query_events(
        actor_type=actor_filter or None,
        action=action_filter or None,
        resource_type=resource_filter or None,
        result=result_filter or None,
        limit=limit,
        offset=offset,
    ) or []
    total = len(all_events)

    rows = "".join(
        f"<tr><td>{e.get('timestamp','')[:19]}</td>"
        f"<td><span class='badge badge-secondary'>{e.get('actor_type','')}</span></td>"
        f"<td><code>{e.get('action','')}</code></td>"
        f"<td>{e.get('resource_type','') or '-'}</td>"
        f"<td><span class='badge badge-{'active' if e.get('result','')=='success' else 'cancelled'}'>{e.get('result','')}</span></td>"
        f"<td class=mono>{e.get('ip_address','') or '-'}</td></tr>"
        for e in all_events
    )

    def build_options(items, selected):
        return "".join(f"<option value='{i}' {'selected' if i==selected else ''}>{i}</option>" for i in items)

    actor_options = build_options(ACTOR_TYPES, actor_filter)
    action_options = build_options(AUDIT_ACTIONS, action_filter)
    result_options = build_options(RESULT_TYPES, result_filter)

    prev_page = page - 1 if page > 1 else 1
    next_page = page + 1
    page_info = f"Page {page}"

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
    </script>"""

    return render_page("Audit Log", f"""
    <div class="page-header"><h1>Audit Log</h1><div class="page-actions">
        <button class="btn btn-secondary" onclick="exportAuditCsv()">Export CSV</button>
    </div></div>
    <div class="card">
      <h3>Events</h3>
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
          <option value="agent" {'selected' if resource_filter=='agent' else ''}>agent</option>
          <option value="workspace" {'selected' if resource_filter=='workspace' else ''}>workspace</option>
          <option value="memory" {'selected' if resource_filter=='memory' else ''}>memory</option>
          <option value="vault" {'selected' if resource_filter=='vault' else ''}>vault</option>
          <option value="activity" {'selected' if resource_filter=='activity' else ''}>activity</option>
        </select>
        <select id="audit-result" style="width:120px">
          <option value="">Any result</option>
          {result_options}
        </select>
        <button class="btn btn-sm" onclick="applyAuditFilters()">Filter</button>
        <button class="btn btn-sm btn-secondary" onclick="clearAuditFilters()">Clear</button>
      </div>
      <table><thead><tr><th>Time</th><th>Actor Type</th><th>Action</th><th>Resource</th><th>Result</th><th>IP</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=6 class=empty>No events yet.</td></tr>"}</tbody></table>
      <div class="pagination" style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <a href="?page={prev_page}{f'&actor_type={actor_filter}' if actor_filter else ''}{f'&action={action_filter}' if action_filter else ''}{f'&resource_type={resource_filter}' if resource_filter else ''}{f'&result={result_filter}' if result_filter else ''}" class="btn btn-sm btn-secondary">Prev</a>
        <span>{page_info}</span>
        <a href="?page={next_page}{f'&actor_type={actor_filter}' if actor_filter else ''}{f'&action={action_filter}' if action_filter else ''}{f'&resource_type={resource_filter}' if resource_filter else ''}{f'&result={result_filter}' if result_filter else ''}" class="btn btn-sm btn-secondary">Next</a>
      </div>
    </div>
    """, "/audit", js, session=session)


# ─── SETTINGS ─────────────────────────────────────────────────────────────────

@router.post("/api/dashboard/system-settings")
async def update_dashboard_system_settings(request: Request, session: dict = Depends(require_admin)):
    from app.database import get_db
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    retention_raw = str(body.get("scratchpad_retention_days", "")).strip()
    solo_raw = str(body.get("solo_mode_enabled", "")).strip().lower()

    try:
        retention_days = int(retention_raw)
    except ValueError:
        return error_response("INVALID_RETENTION", "Scratchpad retention must be a whole number of days", 400)
    if retention_days < 1 or retention_days > 365:
        return error_response("INVALID_RETENTION", "Scratchpad retention must be between 1 and 365 days", 400)
    if solo_raw not in ("true", "false"):
        return error_response("INVALID_SOLO_MODE", "Solo mode must be true or false", 400)

    settings_to_save = {
        "scratchpad_retention_days": str(retention_days),
        "solo_mode_enabled": solo_raw,
    }
    with get_db() as conn:
        for key, value in settings_to_save.items():
            conn.execute(
                """
                INSERT INTO system_settings (key, value, updated_at)
                VALUES (?, ?, CURRENT_TIMESTAMP)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value, updated_at = CURRENT_TIMESTAMP
                """,
                (key, value),
            )
        conn.commit()

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="system_settings",
        result="success",
        details=settings_to_save,
        ip_address=get_client_ip(request),
    )
    return success_response({"settings": settings_to_save})


@router.get("/settings")
async def settings_page(request: Request, session: dict = Depends(require_auth)):
    from app.database import get_db

    user = get_user_by_id(session["user_id"])
    is_admin = session.get("role") == "admin"

    def get_system_setting(key, default):
        with get_db() as conn:
            row = conn.execute("SELECT value FROM system_settings WHERE key = ?", (key,)).fetchone()
            return row["value"] if row else default

    scratchpad_retention_days = get_system_setting("scratchpad_retention_days", "7")
    solo_mode_enabled = get_system_setting("solo_mode_enabled", "true").lower() == "true"

    account_html = f"""
    <div class="two-col">
      <div class="card">
        <h3>Account</h3>
        <p><strong>Display Name:</strong> {user.get('display_name','')}</p>
        <p><strong>Email:</strong> {user.get('email','')}</p>
        <p><strong>Role:</strong> {session.get('role','user')}</p>
      </div>
      <div class="card">
        <h3>Security</h3>
        <div class="form-group">
          <a href="/settings/password" class="btn btn-secondary">Change Password</a>
        </div>
        <div class="form-group">
          <a href="/settings/otp" class="btn btn-secondary">Manage OTP</a>
        </div>
        <div class="form-group">
          <a href="/settings/backup-codes" class="btn btn-secondary">Backup Codes</a>
        </div>
      </div>
    </div>"""

    backup_html = ""
    if is_admin:
        backup_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Backup & Restore</h3>
        <div class="section-actions"><a href="/audit" class="btn btn-sm btn-secondary">Audit Log</a></div>
      </div>
      <div class="form-group">
        <label>OTP or backup code for backup operations:</label>
        <input type="text" id="backup-otp" placeholder="123456 or backup code" autocomplete="one-time-code" style="width:260px">
        <p class="form-hint">Backup codes are single-use and can be used here if your authenticator is unavailable.</p>
      </div>
      <div class="form-group">
        <button class="btn" onclick="exportBackup()">Export Backup</button>
        <button class="btn btn-secondary" onclick="openModal('restore-modal')">Restore from Backup</button>
        <button class="btn btn-secondary" onclick="runMaintenance()">Run Maintenance</button>
      </div>
      <p class="form-hint">Maintenance marks stale activities using <code>AGENT_CORE_STALE_THRESHOLD_MINUTES</code> and deletes active scratchpad memories older than the retention setting below.</p>
      <div id="backup-result" style="margin-top:12px"></div>
      <hr class=divider>
      <h4 class="section-title">Startup Checks</h4>
      <div id="startup-checks"><button class="btn btn-secondary btn-sm" onclick="runStartupChecks()">Run Checks</button></div>
    </div>"""

    vault_key_html = ""
    if is_admin:
        vault_key_html = """
    <div class="card">
      <h3>Vault Key</h3>
      <p class="text-muted" style="margin-bottom:12px">Rotate the vault encryption key or restore a known-good key after confirming OTP.</p>
      <div class="form-group">
        <button class="btn btn-secondary" onclick="loadVaultKeyStatus()">Check Status</button>
        <button class="btn btn-danger" onclick="openModal('vault-key-rotate-modal')">Rotate Vault Key</button>
        <button class="btn btn-secondary" onclick="openModal('vault-key-restore-modal')">Restore Key</button>
      </div>
      <div id="vault-key-result" style="margin-top:12px"></div>
    </div>"""

    broker_html = ""
    if is_admin:
        broker_html = """
    <div class="card">
      <h3>Broker</h3>
      <p class="text-muted" style="margin-bottom:12px">Rotate the broker credential used for resolving AC_SECRET_* references.</p>
        <button class="btn btn-danger" onclick="rotateBroker()">Rotate Broker Credential</button>
      <div id="broker-result" style="margin-top:12px"></div>
    </div>"""

    system_settings_html = ""
    if is_admin:
        solo_checked = "checked" if solo_mode_enabled else ""
        system_settings_html = f"""
    <div class="card">
      <h3>System Behavior</h3>
      <form id="system-settings-form" onsubmit="saveSystemSettings(event)">
        <div class="form-group">
          <label>Scratchpad Retention</label>
          <input type="number" id="scratchpad-retention-days" min="1" max="365" value="{escape_html(scratchpad_retention_days)}" style="width:120px">
          <p class="form-hint">Used by Run Maintenance. Active scratchpad memories older than this many days are permanently deleted.</p>
        </div>
        <label class="checkbox-label">
          <input type="checkbox" id="solo-mode-enabled" {solo_checked}>
          New API-created agents automatically read their owner's user scope
        </label>
        <p class="form-hint">Used when an agent is created without explicit scopes. Existing agents are not changed; edit their access on the Agents page.</p>
        <button type="submit" class="btn">Save Behavior Settings</button>
      </form>
      <div id="system-settings-result" style="margin-top:12px"></div>
    </div>"""

    admin_modals = ""
    if is_admin:
        admin_modals = """
    <!-- Restore Modal -->
    <div class="modal-overlay" id="restore-modal" style="display:none">
      <div class="modal">
        <h3>Restore from Backup</h3>
        <div class="alert alert-danger">This can replace your current database and vault key. Choose merge when you want to preserve current records.</div>
        <form id="restore-form" onsubmit="doRestore(event)">
          <div class="form-group">
            <label>Backup ZIP file *</label>
            <input type="file" id="restore-file" accept=".zip" required>
          </div>
          <div class="form-group">
            <label>Restore Mode *</label>
            <select id="restore-mode" required>
              <option value="replace_all">Replace all current data</option>
              <option value="merge">Merge non-conflicting records</option>
            </select>
          </div>
          <div class="form-group">
            <label>OTP or backup code *</label>
            <input type="text" id="restore-otp" placeholder="123456 or backup code" autocomplete="one-time-code" required>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="vault-key-rotate-modal" style="display:none">
      <div class="modal">
        <h3>Rotate Vault Key</h3>
        <div class="alert alert-danger">This re-encrypts all vault entries with a new primary key. Keep backups of your database and keyring.</div>
        <form id="vault-key-rotate-form" onsubmit="rotateVaultKey(event)">
          <div class="form-group">
            <label>OTP or backup code *</label>
            <input type="text" id="vault-key-rotate-otp" placeholder="123456 or backup code" autocomplete="one-time-code" required>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('vault-key-rotate-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Rotate Key</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="vault-key-restore-modal" style="display:none">
      <div class="modal">
        <h3>Restore Vault Key</h3>
        <form id="vault-key-restore-form" onsubmit="restoreVaultKey(event)">
          <div class="form-group">
            <label>Fernet Key *</label>
            <input type="text" id="vault-key-restore-key" required>
            <p class="form-hint">The key must decrypt all current vault entries before it is accepted.</p>
          </div>
          <div class="form-group">
            <label>OTP or backup code *</label>
            <input type="text" id="vault-key-restore-otp" placeholder="123456 or backup code" autocomplete="one-time-code" required>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('vault-key-restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore Key</button>
          </div>
        </form>
      </div>
    </div>"""

    js = f"""
    <script>
    async function exportBackup() {{
      const otp = document.getElementById('backup-otp').value;
      if (!otp) {{ showToast('Enter OTP or backup code first', 'warning'); return; }}
      const r = await fetch('/api/backup/export', {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify({{ otp_code: otp }})
      }});
      if (r.ok) {{
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'agent-core-backup.zip'; a.click();
        URL.revokeObjectURL(url);
        showToast('Backup downloaded');
      }} else {{
        const j = await r.json();
        showToast(j.error?.message || 'Export failed', 'danger');
      }}
    }}
    async function runMaintenance() {{
      const r = await fetch('/api/backup/maintenance', {{ method: 'POST' }});
      const j = await r.json();
      if (j.ok) {{
        document.getElementById('backup-result').innerHTML =
          '<div class="alert alert-success">Maintenance complete. Stale activities marked: <code>' + j.data.stale_activities_marked + '</code>. Scratchpad memories pruned: <code>' + j.data.scratchpad_pruned + '</code>.</div>';
        showToast('Maintenance complete');
      }} else {{ showToast(j.error?.message || 'Failed', 'danger'); }}
    }}
    async function runStartupChecks() {{
      const r = await fetch('/api/backup/startup-checks');
      const j = await r.json();
      if (j.ok) {{
        const checks = j.data.checks.map(c => '<div style="margin:4px 0"><span class="badge badge-' + (c.status==='OK'?'active':'stale') + '">' + c.status + '</span> ' + c.check + ': ' + c.message + '</div>').join('');
        document.getElementById('startup-checks').innerHTML = checks;
      }} else {{ showToast('Failed', 'danger'); }}
    }}
    async function rotateBroker() {{
      if (!confirm('Rotate broker credential? The new credential will be shown once.')) return;
      const j = await apiFetch('/api/dashboard/broker/rotate', {{ method: 'POST' }});
      if (j.ok) {{
        document.getElementById('broker-result').innerHTML =
          '<div class="alert alert-danger"><strong>Save this credential now - it will not be shown again!</strong></div>' +
          '<div class="api-key-display"><code>' + j.data.credential + '</code></div>' +
          '<button class="copy-btn" onclick="copyToClipboard(\'' + j.data.credential + '\', this)">Copy</button>';
      }} else {{ showToast(j.error.message || 'Failed', 'danger'); }}
    }}
    async function loadVaultKeyStatus() {{
      const j = await apiFetch('/api/vault/rotate/status');
      if (j.ok) {{
        const s = j.data.vault_key_status;
        document.getElementById('vault-key-result').innerHTML =
          '<div class="alert alert-success">Mode: <code>' + s.mode + '</code> · Keyring size: <code>' + s.keyring_size + '</code> · Primary key ID: <code>' + s.primary_key_id + '</code></div>';
      }} else {{ showToast(j.error?.message || 'Status failed', 'danger'); }}
    }}
    async function rotateVaultKey(e) {{
      e.preventDefault();
      const otp = document.getElementById('vault-key-rotate-otp').value;
      if (!otp) {{ showToast('Enter OTP or backup code', 'warning'); return; }}
      const j = await apiFetch('/api/vault/rotate', {{
        method: 'POST',
        body: JSON.stringify({{ otp_code: otp }})
      }});
      if (j.ok) {{
        closeModal('vault-key-rotate-modal');
        document.getElementById('vault-key-rotate-form').reset();
        document.getElementById('vault-key-result').innerHTML =
          '<div class="alert alert-success">' + j.data.message + ' Re-encrypted: <code>' + j.data.re_encrypted_count + '</code>. Keyring size: <code>' + j.data.keyring_size + '</code>.</div>';
      }} else {{ showToast(j.error?.message || 'Rotation failed', 'danger'); }}
    }}
    async function restoreVaultKey(e) {{
      e.preventDefault();
      const key = document.getElementById('vault-key-restore-key').value.trim();
      const otp = document.getElementById('vault-key-restore-otp').value;
      if (!key || !otp) {{ showToast('Enter key and OTP or backup code', 'warning'); return; }}
      const j = await apiFetch('/api/vault/restore-key', {{
        method: 'POST',
        body: JSON.stringify({{ key_base64: key, otp_code: otp }})
      }});
      if (j.ok) {{
        closeModal('vault-key-restore-modal');
        document.getElementById('vault-key-restore-form').reset();
        document.getElementById('vault-key-result').innerHTML = '<div class="alert alert-success">' + j.data.message + '</div>';
      }} else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function doRestore(e) {{
      e.preventDefault();
      const formData = new FormData();
      const file = document.getElementById('restore-file').files[0];
      const otp = document.getElementById('restore-otp').value;
      const mode = document.getElementById('restore-mode').value;
      if (!file) {{ showToast('Select a backup file', 'warning'); return; }}
      if (!otp) {{ showToast('Enter OTP or backup code', 'warning'); return; }}
      formData.append('backup', file);
      formData.append('otp_code', otp);
      formData.append('mode', mode);
      const r = await fetch('/api/backup/restore', {{ method: 'POST', body: formData }});
      const j = await r.json();
      if (j.ok) {{ showToast('Restore complete (' + j.data.mode + ')'); closeModal('restore-modal'); }}
      else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function saveSystemSettings(e) {{
      e.preventDefault();
      const body = {{
        scratchpad_retention_days: document.getElementById('scratchpad-retention-days').value,
        solo_mode_enabled: document.getElementById('solo-mode-enabled').checked ? 'true' : 'false',
      }};
      const j = await apiFetch('/api/dashboard/system-settings', {{
        method: 'POST',
        body: JSON.stringify(body)
      }});
      if (j.ok) {{
        document.getElementById('system-settings-result').innerHTML = '<div class="alert alert-success">Behavior settings saved.</div>';
      }} else {{
        showToast(j.error?.message || 'Failed to save settings', 'danger');
      }}
    }}
    </script>"""

    return render_page("Settings", f"""
    <div class="page-header"><h1>Settings</h1></div>
    {account_html}
    {system_settings_html}
    {vault_key_html}
    {broker_html}
    {backup_html}
    {admin_modals}
    """, "/settings", js, session=session)


# ─── SETTINGS PASSWORD ──────────────────────────────────────────────────────────

@router.get("/settings/password")
async def settings_password_page(request: Request, session: dict = Depends(require_auth)):
    js = """
    <script>
    async function submitPassword(e) {
      e.preventDefault();
      const current = document.getElementById('current-password').value;
      const new_pw = document.getElementById('new-password').value;
      const confirm = document.getElementById('confirm-password').value;
      if (new_pw !== confirm) { showToast('New passwords do not match', 'warning'); return; }
      if (new_pw.length < 8) { showToast('Password must be at least 8 characters', 'warning'); return; }
      const j = await apiFetch('/api/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: new_pw })
      });
      if (j.ok) {
        showToast('Password updated', 'success');
        e.target.reset();
      } else {
        showToast(j.error?.message || 'Failed', 'danger');
      }
    }
    </script>"""
    return render_page("Change Password", """
    <div class="page-header"><h1>Change Password</h1></div>
    <div class="card" style="max-width:500px">
      <h3>Update Your Password</h3>
      <form id="password-form" onsubmit="submitPassword(event)">
        <div class="form-group">
          <label>Current Password</label>
          <input type="password" id="current-password" required>
        </div>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" id="new-password" minlength="8" required>
        </div>
        <div class="form-group">
          <label>Confirm New Password</label>
          <input type="password" id="confirm-password" minlength="8" required>
        </div>
        <button type="submit" class="btn">Update Password</button>
      </form>
    </div>
    """, "/settings", js, session=session)


# ─── SETTINGS OTP ───────────────────────────────────────────────────────────────

@router.get("/settings/otp")
async def settings_otp_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import is_otp_enrolled
    enrolled = is_otp_enrolled(session["user_id"])

    if enrolled:
        body_html = """
        <div class="alert alert-success">OTP is currently enrolled on your account.</div>
        <p class="text-muted">Resetting OTP requires your current password and an existing authenticator code. Your current OTP stays active until the new code is verified.</p>
        <form id="otp-reset-form" onsubmit="submitOtpReset(event)">
          <div class="form-group">
            <label for="otp-current-password">Current Password</label>
            <input type="password" id="otp-current-password" autocomplete="current-password" required>
          </div>
          <div class="form-group">
            <label for="otp-current-code">Current OTP Code</label>
            <input type="text" id="otp-current-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" required style="width:160px">
          </div>
          <button type="submit" class="btn btn-danger">Reset OTP</button>
        </form>
        <div id="otp-reset-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function submitOtpReset(e) {
          e.preventDefault();
          if (!confirm('Reset OTP? Existing authenticator setup and backup codes will be replaced.')) return;
          await startOtpEnrollment('otp-reset-result');
        }
        </script>"""
    else:
        body_html = """
        <div class="alert alert-warning">OTP is not currently enrolled.</div>
        <p class="text-muted">Enrolling OTP adds a second layer of authentication. Enter your current password, then scan the QR code with an authenticator app.</p>
        <div class="form-group">
          <label for="otp-current-password">Current Password</label>
          <input type="password" id="otp-current-password" autocomplete="current-password" required>
        </div>
        <button class="btn" onclick="enrollOtp()">Start OTP Setup</button>
        <div id="otp-enroll-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function enrollOtp() {
          await startOtpEnrollment('otp-enroll-result');
        }
        </script>"""

    setup_js = """
        <script>
        async function startOtpEnrollment(targetId) {
          const target = document.getElementById(targetId);
          const password = document.getElementById('otp-current-password')?.value || '';
          const currentOtp = document.getElementById('otp-current-code')?.value || '';
          if (!password) { showToast('Enter your current password', 'warning'); return; }
          target.innerHTML = '<div class="alert alert-warning">Preparing OTP setup...</div>';
          const body = { current_password: password };
          if (currentOtp) body.otp_code = currentOtp;
          const j = await apiFetch('/api/auth/otp/enroll', {
            method: 'POST',
            body: JSON.stringify(body)
          });
          if (j.ok) {
            const qrHtml = j.data.qr_svg
              ? '<img class="otp-qr" src="' + j.data.qr_svg + '" alt="OTP enrollment QR code">'
              : '<div class="alert alert-warning">QR code could not be generated. Use the manual secret instead.</div>';
            target.innerHTML =
              '<div class="alert alert-success"><strong>Scan this QR code in your authenticator app.</strong></div>' +
              '<div class="otp-enroll-layout">' +
                qrHtml +
                '<div>' +
                  '<p class="text-muted">Or enter this secret manually:</p>' +
                  '<div class="mono" style="word-break:break-all;margin:12px 0">' + escapeHtml(j.data.secret) + '</div>' +
                  '<label for="otp-confirm-code">Enter the 6-digit code from your app</label>' +
                  '<input type="text" id="otp-confirm-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" style="width:160px">' +
                  '<div style="margin-top:10px"><button class="btn" onclick="confirmOtpEnrollment(\\'' + targetId + '\\')">Verify and Enable OTP</button></div>' +
                '</div>' +
              '</div>';
          } else {
            target.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Failed to prepare OTP setup') + '</div>';
            showToast(j.error?.message || 'Failed', 'danger');
          }
        }
        async function confirmOtpEnrollment(targetId) {
          const code = document.getElementById('otp-confirm-code')?.value || '';
          if (!code) { showToast('Enter the authenticator code', 'warning'); return; }
          const j = await apiFetch('/api/auth/otp/confirm', {
            method: 'POST',
            body: JSON.stringify({ otp_code: code })
          });
          if (j.ok) {
            document.getElementById(targetId).innerHTML =
              '<div class="alert alert-success"><strong>OTP is enabled.</strong></div>' +
              '<div class="alert alert-danger"><strong>Save these backup codes now - they will not be shown again.</strong></div>' +
              '<div class="mono">' + j.data.backup_codes.join('<br>') + '</div>';
          } else {
            showToast(j.error?.message || 'Invalid code', 'danger');
          }
        }
        </script>
    """
    js = setup_js + js

    return render_page("Manage OTP", f"""
    <div class="page-header"><h1>Two-Factor Authentication</h1></div>
    <div class="card" style="max-width:600px">
      {body_html}
    </div>
    """, "/settings", js, session=session)


# ─── SETTINGS BACKUP CODES ────────────────────────────────────────────────────

@router.get("/settings/backup-codes")
async def settings_backup_codes_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import is_otp_enrolled

    enrolled = is_otp_enrolled(session["user_id"])
    codes_html = ""
    if enrolled:
        codes_html = """
        <div class="alert alert-warning">Existing backup codes cannot be viewed again. Regenerate to invalidate old codes and receive a new one-time list.</div>
        <button class="btn" onclick="regenerateCodes()">Regenerate Backup Codes</button>"""

    js = f"""
    <script>
    async function regenerateCodes() {{
      if (!confirm('This will invalidate all existing backup codes. Continue?')) return;
      const j = await apiFetch('/api/auth/otp/backup-codes', {{ method: 'POST' }});
      if (j.ok) {{
        document.querySelector('.card').innerHTML +=
          '<div class="alert alert-danger" style="margin-top:12px"><strong>New codes - save these now!</strong></div>' +
          '<div class="mono">' + j.data.backup_codes.join('<br>') + '</div>';
        showToast('Backup codes regenerated', 'success');
      }} else {{
        showToast(j.error?.message || 'Failed', 'danger');
      }}
    }}
    </script>"""

    return render_page("Backup Codes", f"""
    <div class="page-header"><h1>Backup Codes</h1></div>
    <div class="card" style="max-width:600px">
      <h3>Recovery Codes</h3>
      <p class="text-muted">Backup codes are used to access your account if you lose access to your authenticator app. Each code can only be used once.</p>
      {"<div class='alert alert-warning' style='margin-top:12px'>You must enroll OTP before you can regenerate backup codes.</div>" if not enrolled else ""}
      {codes_html}
    </div>
    """, "/settings", js, session=session)


# ─── INTEGRATIONS ─────────────────────────────────────────────────────────────

@router.get("/integrations")
async def integrations_page(request: Request, session: dict = Depends(require_auth)):
    return RedirectResponse(url="/agent-setup", status_code=302)


@router.post("/api/agent-setup/apply-recommended-access")
@router.post("/agent-setup/apply-access")
async def apply_recommended_access(
    request: Request,
    session: dict = Depends(require_auth),
):
    from app.services import agent_service
    from app.services import workspace_service
    from pydantic import BaseModel

    class ApplyAccessBody(BaseModel):
        user_id: str
        workspace_id: str
        agent_id: str
        include_user_write: bool = False

    body = ApplyAccessBody.model_validate(await request.json())

    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and current_user_id != body.user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    user = get_user_by_id(body.user_id)
    if not user:
        return error_response("NOT_FOUND", "User not found", 404)

    workspace = workspace_service.get_workspace_by_id(body.workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not is_admin and workspace.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response("AGENT_USER_MISMATCH", "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.", 400)

    agent_scope = f"agent:{body.agent_id}"
    workspace_scope = f"workspace:{body.workspace_id}"
    user_scope = f"user:{body.user_id}"

    current_read = agent.get("read_scopes_json", "[]")
    current_write = agent.get("write_scopes_json", "[]")

    from app.services.agent_service import parse_scopes
    read_scopes = parse_scopes(current_read) if current_read else []
    write_scopes = parse_scopes(current_write) if current_write else []

    def add_scope(scopes, scope):
        if scope not in scopes:
            scopes.append(scope)

    add_scope(read_scopes, agent_scope)
    add_scope(write_scopes, agent_scope)
    add_scope(read_scopes, workspace_scope)
    add_scope(write_scopes, workspace_scope)
    add_scope(read_scopes, user_scope)
    if body.include_user_write:
        add_scope(write_scopes, user_scope)

    agent_service.update_agent(
        body.agent_id,
        read_scopes=read_scopes,
        write_scopes=write_scopes,
    )

    from app.services import audit_service
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="scope_grant",
        resource_type="agent",
        resource_id=body.agent_id,
        result="success",
        details={
            "workspace_id": body.workspace_id,
            "user_id": body.user_id,
            "new_read_scopes": read_scopes,
            "new_write_scopes": write_scopes,
            "include_user_write": body.include_user_write,
        },
    )

    return success_response({
        "message": "Access updated",
        "read_scopes": read_scopes,
        "write_scopes": write_scopes,
    })


@router.post("/api/agent-setup/verify")
async def verify_agent_setup(
    request: Request,
    session: dict = Depends(require_auth),
):
    from pydantic import BaseModel

    class VerifyBody(BaseModel):
        user_id: str
        workspace_id: str
        agent_id: str
        write_test_memory: bool = False

    body = VerifyBody.model_validate(await request.json())

    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and current_user_id != body.user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    from app.services import agent_service, workspace_service, audit_service

    workspace = workspace_service.get_workspace_by_id(body.workspace_id)
    if not workspace:
        return error_response("NOT_FOUND", "Workspace not found", 404)

    if not is_admin and workspace.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response("AGENT_USER_MISMATCH", "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.", 400)

    from app.services.agent_service import parse_scopes
    read_scopes = parse_scopes(agent.get("read_scopes_json", "[]"))
    write_scopes = parse_scopes(agent.get("write_scopes_json", "[]"))
    workspace_ids = {
        scope.split(":", 1)[1]
        for scope in read_scopes + write_scopes
        if scope.startswith("workspace:") and ":" in scope
    }
    enforcer = ScopeEnforcer(
        read_scopes,
        write_scopes,
        body.agent_id,
        is_admin=is_admin,
        active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
    )

    workspace_scope = f"workspace:{body.workspace_id}"
    user_scope = f"user:{body.user_id}"
    agent_scope = f"agent:{body.agent_id}"

    checks = []
    all_ok = True

    try:
        transport = httpx.ASGITransport(app=request.app)
        async with httpx.AsyncClient(
            transport=transport,
            base_url=str(request.base_url).rstrip("/"),
        ) as client:
            health_r = await client.get(
                "/health",
                timeout=5.0,
            )
            manifest_r = await client.get(
                "/mcp",
                timeout=5.0,
            )
        if health_r.status_code == 200:
            checks.append({"check": "api_connectivity", "status": "ok", "message": "Agent Core API reachable"})
        else:
            checks.append({"check": "api_connectivity", "status": "error", "message": f"Agent Core API returned {health_r.status_code}"})
            all_ok = False

        if manifest_r.status_code in (200, 401, 403):
            checks.append({"check": "mcp_connectivity", "status": "ok", "message": "MCP endpoint reachable"})
        else:
            checks.append({"check": "mcp_connectivity", "status": "error", "message": f"MCP endpoint returned {manifest_r.status_code}"})
            all_ok = False
    except Exception as e:
        checks.append({"check": "api_connectivity", "status": "error", "message": f"Agent Core API unreachable ({type(e).__name__})"})
        checks.append({"check": "mcp_connectivity", "status": "error", "message": f"MCP endpoint unreachable ({type(e).__name__})"})
        all_ok = False

    if enforcer.can_read(workspace_scope):
        checks.append({"check": "workspace_read", "status": "ok", "message": f"Read access to {workspace_scope}"})
    else:
        checks.append({"check": "workspace_read", "status": "error", "message": f"No read access to {workspace_scope}"})
        all_ok = False

    if enforcer.can_write(workspace_scope):
        checks.append({"check": "workspace_write", "status": "ok", "message": f"Write access to {workspace_scope}"})
        if body.write_test_memory:
            record, _ = _write_test_memory(
                scope=workspace_scope,
                content=f"Agent Core setup verification for agent:{body.agent_id} user:{body.user_id} workspace:{body.workspace_id}",
                memory_class="fact",
            )
            if record:
                checks.append({"check": "memory_write", "status": "ok", "message": f"Verified write to {workspace_scope}", "record_id": record["id"]})
            else:
                checks.append({"check": "memory_write", "status": "error", "message": f"Failed to write to {workspace_scope}"})
                all_ok = False
        else:
            checks.append({"check": "memory_write", "status": "skipped", "message": "Test memory write skipped"})
    else:
        checks.append({"check": "workspace_write", "status": "error", "message": f"No write access to {workspace_scope}"})
        checks.append({"check": "memory_write", "status": "blocked", "message": "Cannot test write without workspace write access"})
        all_ok = False

    if enforcer.can_read(user_scope):
        checks.append({"check": "user_read", "status": "ok", "message": f"Read access to {user_scope}"})
    elif is_admin or agent.get("owner_user_id") == current_user_id:
        checks.append({"check": "user_read", "status": "warning", "message": f"No read access to {user_scope} (optional)"})
    else:
        checks.append({"check": "user_read", "status": "warning", "message": f"No read access to {user_scope} (optional)"})

    if enforcer.can_read(agent_scope) and enforcer.can_write(agent_scope):
        checks.append({"check": "agent_scope", "status": "ok", "message": f"Private scope {agent_scope} available"})
    else:
        checks.append({"check": "agent_scope", "status": "warning", "message": f"Private scope {agent_scope} incomplete or missing"})

    from app.services import audit_service
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="setup_verification",
        resource_type="agent",
        resource_id=body.agent_id,
        result="success" if all_ok else "failure",
        details={
            "workspace_id": body.workspace_id,
            "user_id": body.user_id,
            "checks": checks,
        },
    )

    return success_response({
        "ok": all_ok,
        "checks": checks,
    })


def _write_test_memory(scope, content, memory_class):
    try:
        from app.services import memory_service
        record, _ = memory_service.write_memory(
            content=content,
            memory_class=memory_class,
            scope=scope,
            domain="setup",
            topic="verification",
            confidence=0.9,
            importance=0.3,
            source_kind="tool_output",
        )
        return record, False
    except Exception:
        return None, False


def _agent_context_user_id(agent):
    return agent.get("default_user_id") or agent.get("owner_user_id")


def _agent_user_matches(agent, user_id):
    return _agent_context_user_id(agent) == user_id


def _agent_setup_output_options(target=None):
    return [
        ("instructions", "User Instructions", "agent-core-user-instructions.md"),
        ("mcp_json", "MCP Config", "agent-core-mcp-config.txt"),
        ("env", "Environment Variables (optional key storage)", "agent-core.env"),
        ("session", "Session Prompt", "agent-core-session-prompt.md"),
        ("claude_md", "Workspace CLAUDE.md", "CLAUDE.md"),
        ("agents_md", "Workspace AGENTS.md", "AGENTS.md"),
        ("verification", "Verification Prompt", "agent-core-verification.md"),
    ]


def _agent_setup_target_label(target):
    return {
        "claude_code": "Claude Code",
        "codex": "Codex",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "generic_mcp": "Generic MCP",
    }.get(target, "Generic MCP")


def _agent_setup_access_model(
    agent,
    workspace,
    user_id,
    is_admin=False,
):
    from app.services import workspace_service
    from app.services.agent_service import parse_scopes

    agent_id = agent["id"]
    workspace_scope = f"workspace:{workspace['id']}" if workspace else ""
    user_scope = f"user:{user_id}"
    agent_scope = f"agent:{agent_id}"
    read_scopes = parse_scopes(agent.get("read_scopes_json", "[]"))
    write_scopes = parse_scopes(agent.get("write_scopes_json", "[]"))
    workspace_ids = {
        scope.split(":", 1)[1]
        for scope in read_scopes + write_scopes
        if scope.startswith("workspace:") and ":" in scope
    }
    enforcer = ScopeEnforcer(
        read_scopes,
        write_scopes,
        agent_id,
        active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
    )
    checks = []

    checks.append({"label": "Agent active", "status": "ok" if agent.get("is_active") else "blocked"})
    if workspace:
        if workspace.get("is_active"):
            checks.append({"label": "Workspace active", "status": "ok"})
        else:
            checks.append({"label": "Workspace inactive", "status": "warning"})

        can_read_workspace = enforcer.can_read(workspace_scope)
        can_write_workspace = enforcer.can_write(workspace_scope)
        if can_read_workspace and can_write_workspace:
            checks.append({"label": "Workspace read/write access", "status": "ok"})
        elif can_read_workspace:
            checks.append({"label": "Workspace read-only access", "status": "warning"})
        else:
            checks.append({"label": "No workspace access", "status": "blocked"})
            checks.append({"label": "Recommended: add workspace scope to agent", "status": "warning"})
    else:
        checks.append({"label": "No workspace selected", "status": "info"})

    can_read_user = enforcer.can_read(user_scope)
    can_write_user = enforcer.can_write(user_scope)
    if can_read_user and can_write_user:
        checks.append({"label": "User preference read/write access", "status": "warning"})
        checks.append({"label": "Warning: user-scope write access granted", "status": "warning"})
    elif can_read_user:
        checks.append({"label": "User preference read access", "status": "ok"})
    else:
        checks.append({"label": "No user preference access", "status": "warning"})
        checks.append({"label": "Recommended: add user scope to agent", "status": "warning"})

    if enforcer.can_read(agent_scope) and enforcer.can_write(agent_scope):
        checks.append({"label": "Agent private scope", "status": "ok"})
    else:
        checks.append({"label": "Agent private scope incomplete", "status": "warning"})

    if workspace_scope and enforcer.can_read(workspace_scope):
        checks.append({"label": "Vault access (workspace scope)", "status": "ok"})
    elif enforcer.can_read("shared") or enforcer.can_read(user_scope):
        checks.append({"label": "Vault access (user/shared scope)", "status": "ok"})
    else:
        checks.append({"label": "Vault access", "status": "warning"})

    if agent.get("owner_user_id") == user_id or is_admin:
        checks.append({"label": "Activity tracking", "status": "ok"})
    else:
        checks.append({"label": "Activity tracking (limited)", "status": "warning"})

    checks.append({"label": "Scope model: global agent scopes, not per-workspace", "status": "info"})
    recommended_read = read_scopes + [agent_scope, user_scope]
    recommended_write = write_scopes + [agent_scope]
    if workspace_scope:
        recommended_read.append(workspace_scope)
        recommended_write.append(workspace_scope)
    recommended = {
        "read": sorted(set(recommended_read)),
        "write": sorted(set(recommended_write)),
    }
    return checks, recommended


@router.post("/api/agent-setup/preview")
async def preview_agent_setup(
    request: Request,
    session: dict = Depends(require_auth),
):
    from pydantic import BaseModel
    from app.services.agent_service import list_agents, parse_scopes
    from app.services import workspace_service
    from app.database import get_db

    class PreviewBody(BaseModel):
        user_id: str
        workspace_id: str = ""
        agent_id: str
        target: str = "claude_code"
        output_type: str = "claude_md"

    body = PreviewBody.model_validate(await request.json())
    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and body.user_id != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?",
            (body.user_id,),
        ).fetchone()
    if not user_row:
        return error_response("NOT_FOUND", "User not found", 404)
    user = dict(user_row)

    workspace = None
    if body.workspace_id:
        workspace = workspace_service.get_workspace_by_id(body.workspace_id)
        if not workspace:
            return error_response("NOT_FOUND", "Workspace not found", 404)
        if not is_admin and workspace.get("owner_user_id") != current_user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

    agents = list_agents() if is_admin else list_agents(owner_user_id=current_user_id)
    agent = next((a for a in agents if a["id"] == body.agent_id and a.get("is_active")), None)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not _agent_user_matches(agent, body.user_id):
        return error_response("AGENT_USER_MISMATCH", "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.", 400)

    access_checks, recommended = _agent_setup_access_model(
        agent=agent,
        workspace=workspace,
        user_id=body.user_id,
        is_admin=is_admin,
    )
    base_url = str(request.base_url).rstrip("/")
    outputs = {}
    for output_type, _label, _filename in _agent_setup_output_options(body.target):
        _out_label, output = _build_agent_setup_output(
            user=user,
            workspace=workspace,
            agent=agent,
            target=body.target,
            output_type=output_type,
            base_url=base_url,
        )
        outputs[output_type] = output

    return success_response({
        "recommended_scopes": recommended,
        "access_checks": access_checks,
        "outputs": outputs,
        "selected_output": outputs.get(body.output_type, outputs.get("instructions", "")),
    })


@router.post("/api/agent-setup/generate-connection")
async def generate_agent_connection(
    request: Request,
    session: dict = Depends(require_auth),
):
    from pydantic import BaseModel
    from app.services import agent_service, workspace_service, audit_service
    from app.database import get_db

    class GenerateConnectionBody(BaseModel):
        user_id: str
        workspace_id: str = ""
        agent_id: str
        target: str = "claude_code"
        output_type: str = "env"

    body = GenerateConnectionBody.model_validate(await request.json())
    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    if not is_admin and body.user_id != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)

    with get_db() as conn:
        user_row = conn.execute(
            "SELECT id, email, display_name FROM users WHERE id = ?",
            (body.user_id,),
        ).fetchone()
    if not user_row:
        return error_response("NOT_FOUND", "User not found", 404)
    user = dict(user_row)

    workspace = None
    if body.workspace_id:
        workspace = workspace_service.get_workspace_by_id(body.workspace_id)
        if not workspace:
            return error_response("NOT_FOUND", "Workspace not found", 404)
        if not is_admin and workspace.get("owner_user_id") != current_user_id:
            return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not agent.get("is_active"):
        return error_response("AGENT_INACTIVE", "Cannot generate config for inactive agent", 400)
    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response("AGENT_USER_MISMATCH", "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.", 400)

    api_key = agent_service.rotate_agent_key(agent["id"])
    if not api_key:
        return error_response("AGENT_INACTIVE", "Cannot rotate key for inactive agent", 400)

    output_label, output = _build_agent_setup_output(
        user=user,
        workspace=workspace,
        agent=agent,
        target=body.target,
        output_type=body.output_type,
        base_url=str(request.base_url).rstrip("/"),
        api_key=api_key,
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="agent_key_rotated",
        resource_type="agent",
        resource_id=agent["id"],
        result="success",
        details={
            "user_id": body.user_id,
            "workspace_id": body.workspace_id,
            "target": body.target,
            "output_type": body.output_type,
        },
    )

    filename = next((f for v, _l, f in _agent_setup_output_options(body.target) if v == body.output_type), "agent-core-output.txt")
    return success_response({
        "agent_id": agent["id"],
        "output_type": body.output_type,
        "output_label": output_label,
        "filename": filename,
        "output": output,
        "api_key": api_key,
        "warning": "This key is shown once. Generating again rotates the agent key and invalidates the previous key.",
    })


@router.get("/agent-setup")
async def agent_setup_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    from app.services.agent_service import list_agents, parse_scopes
    from app.services import workspace_service
    from app.database import get_db

    is_admin = session.get("role") == "admin"
    current_user_id = session["user_id"]

    user_id = request.query_params.get("user_id", current_user_id)
    workspace_id = request.query_params.get("workspace_id", "")
    agent_id = request.query_params.get("agent_id", "")
    target = request.query_params.get("target", "generic_mcp")
    output_type = request.query_params.get("output_type", "instructions")
    tool_label = _agent_setup_target_label(target)

    users = []
    with get_db() as conn:
        if is_admin:
            rows = conn.execute("SELECT id, email, display_name FROM users ORDER BY display_name").fetchall()
        else:
            rows = conn.execute(
                "SELECT id, email, display_name FROM users WHERE id = ?",
                (current_user_id,),
            ).fetchall()
    users = [{"id": r["id"], "email": r["email"], "display_name": r["display_name"]} for r in rows]

    workspaces = workspace_service.list_workspaces() if is_admin else workspace_service.list_workspaces(owner_user_id=current_user_id)
    project_options = "".join(
        f'<option value="{p["id"]}" {"selected" if p["id"] == workspace_id else ""}>{escape_html(p["name"])}</option>'
        for p in workspaces
    )

    agents = list_agents() if is_admin else list_agents(owner_user_id=current_user_id)
    visible_agents = {a["id"]: a for a in agents if a.get("is_active")}
    agent_options = "".join(
        f'<option value="{a["id"]}" {"selected" if a["id"] == agent_id else ""}>{escape_html(a.get("display_name", a["id"]))}</option>'
        for a in visible_agents.values()
    )

    user_options = "".join(
        f'<option value="{u["id"]}" {"selected" if u["id"] == user_id else ""}>{escape_html(u.get("display_name", u["id"]))}</option>'
        for u in users
    )

    output_options = [(v, label) for v, label, _filename in _agent_setup_output_options(target)]

    access_checks = []
    generated_output = ""
    output_label = ""
    base_url = str(request.base_url).rstrip("/")
    recommended_scopes = None

    if agent_id:
        agent = visible_agents.get(agent_id)
        workspace = next((p for p in workspaces if p["id"] == workspace_id), None) if workspace_id else None
        user = next((u for u in users if u["id"] == user_id), None)

        if agent and agent.get("is_active"):
            access_checks.append({"label": "Agent active", "status": "ok"})
        elif agent:
            access_checks.append({"label": "Agent inactive", "status": "blocked"})
        else:
            access_checks.append({"label": "Agent not found", "status": "blocked"})

        if not workspace_id:
            access_checks.append({"label": "Workspace optional for this output", "status": "info"})
        elif workspace and workspace.get("is_active"):
            access_checks.append({"label": "Workspace active", "status": "ok"})
        elif workspace:
            access_checks.append({"label": "Workspace inactive", "status": "warning"})
        else:
            access_checks.append({"label": "Workspace not found", "status": "blocked"})

        if agent:
            access_checks, recommended_scopes = _agent_setup_access_model(
                agent=agent,
                workspace=workspace,
                user_id=user_id,
                is_admin=is_admin,
            )

        if agent and user:
            output_label, generated_output = _build_agent_setup_output(
                user=user,
                workspace=workspace,
                agent=agent,
                target=target,
                output_type=output_type,
                base_url=base_url,
            )

    checks_html = ""
    recommended_html = ""
    if access_checks:
        checks_html = "<div class='access-checks'>"
        for check in access_checks:
            cls = {"ok": "check-ok", "warning": "check-warn", "blocked": "check-blocked", "info": "check-info"}[check["status"]]
            icon = {"ok": "&#10003;", "warning": "&#9888;", "blocked": "&#10007;", "info": "&#8505;"}[check["status"]]
            checks_html += f"<div class='{cls}'><span class='check-icon'>{icon}</span>{escape_html(check['label'])}</div>"
        checks_html += "</div>"
        if any(c["status"] == "warning" for c in access_checks):
            checks_html += "<p class='text-muted' style='font-size:0.8rem;margin-top:8px'>Warnings indicate missing access. Review access on the Agents page or generate the prompt anyway.</p>"

        agent_scope = f"agent:{agent_id}"
        workspace_scope = f"workspace:{workspace_id}" if workspace_id else ""
        user_scope = f"user:{user_id}"
        current_read = parse_scopes(agent.get("read_scopes_json", "[]")) if agent else []
        current_write = parse_scopes(agent.get("write_scopes_json", "[]")) if agent else []
        rec_read_default = current_read + [agent_scope, user_scope]
        rec_write_default = current_write + [agent_scope]
        if workspace_scope:
            rec_read_default.append(workspace_scope)
            rec_write_default.append(workspace_scope)
        rec_read = recommended_scopes["read"] if recommended_scopes is not None else list(set(rec_read_default))
        rec_write = recommended_scopes["write"] if recommended_scopes is not None else list(set(rec_write_default))

        rec_read_display = ", ".join(f"<code>{s}</code>" for s in sorted(rec_read) if s not in current_read)
        rec_write_display = ", ".join(f"<code>{s}</code>" for s in sorted(rec_write) if s not in current_write)

        if rec_read_display or rec_write_display:
            recommended_html = f"""
            <div class="access-apply-section">
              <h2>Recommended Access</h2>
              <p class="text-muted" style="font-size:0.82rem;margin-bottom:12px">
                Granting access applies globally. Workspace access is only added when a workspace is selected.
              </p>
              <div class="scope-compare">
                <div>
                  <strong>Read scopes to add:</strong>
                  <div class="scope-tags">{rec_read_display or "<em>none</em>"}</div>
                </div>
                <div>
                  <strong>Write scopes to add:</strong>
                  <div class="scope-tags">{rec_write_display or "<em>none</em>"}</div>
                </div>
              </div>
              <div class="apply-controls">
                <label class="checkbox-label" style="margin-bottom:12px">
                  <input type="checkbox" id="include-user-write"> Also grant write access to user scope (allows agent to write preferences)
                </label>
                <button class="btn btn-sm btn-warning" id="apply-access-btn" onclick="applyRecommendedAccess()">Apply Recommended Access</button>
                <span id="apply-status" style="margin-left:10px;font-size:0.8rem"></span>
              </div>
            </div>
            """

    output_tabs = ""
    current_params = {
        "user_id": user_id,
        "workspace_id": workspace_id,
        "agent_id": agent_id,
    }
    from urllib.parse import urlencode
    for value, label, _filename in _agent_setup_output_options(target):
        params = dict(current_params)
        params["output_type"] = value
        active = "active" if value == output_type else ""
        output_tabs += f'<a class="setup-tab {active}" href="/agent-setup?{urlencode(params)}">{escape_html(label)}</a>\n'
    filename = next((f for v, _l, f in _agent_setup_output_options(target) if v == output_type), "agent-core-output.txt")

    if generated_output:
        output_display = f"<pre class='output-block'>{escape_html(generated_output)}</pre>"
        copy_btn = "<button type='button' class='btn btn-sm btn-secondary' onclick=\"copyGeneratedOutput(this)\">Copy</button>"
        download_btn = f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"downloadCurrentOutput('{escape_html(filename)}')\">Download</button>"
        regenerate_btn = "<button type='submit' class='btn btn-sm btn-secondary'>Regenerate</button>"
        connection_label = "Generate One-Time Key + MCP Config" if output_type == "mcp_json" else "Generate One-Time Key + Environment Variables"
        connection_btn = (
            f"<button type='button' class='btn btn-sm btn-warning' id='generate-connection-btn' data-label='{escape_html(connection_label)}' onclick='generateConnectionConfig()'>{escape_html(connection_label)}</button>"
            if output_type in ("env", "mcp_json")
            else ""
        )
    else:
        output_display = "<div class='empty'>Select a user and agent to generate setup output. Select a workspace only when you want workspace-specific memory guidance.</div>"
        copy_btn = ""
        download_btn = ""
        regenerate_btn = ""
        connection_btn = ""

    destination_guidance = _get_destination_guidance(target, output_type)

    verify_section_display = "block" if (workspace_id and agent_id) else "none"
    verify_section_html = f"""
      <div class="form-section" id="verify-section" style="display:{verify_section_display}">
        <h2>Setup Verification</h2>
        <div id="verify-results"></div>
        <label class="checkbox-label" style="margin-bottom:12px">
          <input type="checkbox" id="write-test-memory"> Write a setup verification memory record to the workspace scope
        </label>
        <button class="btn btn-sm btn-primary" id="verify-btn" onclick="runSetupVerification()">Run Setup Check</button>
      </div>
    """

    access_check_section = f'<div class="form-section"><h2>Access Check</h2>{checks_html}</div>' if checks_html else ""
    destination_section = f'<div class="form-section"><h2>Destination</h2><p>{destination_guidance}</p></div>' if destination_guidance else ""
    output_label_html = f"<div class='output-label'>{output_label}</div>" if output_label else ""

    body = f"""
    <div class="page-header setup-page-header">
      <h1>Integrations</h1>
      <p class="subtitle">Generate user setup steps, environment variables, MCP config, and AI-facing prompts for connecting tools to Agent Core.</p>
      <div class="text-muted" style="font-size:0.86rem;margin-top:8px">
        Current tool preset: <strong>{escape_html(tool_label)}</strong>. First-class presets are Claude Code, Codex, Cursor, Windsurf, and Generic MCP/REST. Claude Desktop and OpenClaw are static examples in <code>templates/integrations/</code>.
      </div>
    </div>

    <form method="get" action="/agent-setup#generated-output" class="setup-form">
      <div class="form-section">
        <h2>Choose Context</h2>
        <div class="form-row">
          <div class="form-group">
            <label for="user_id">User</label>
            <select id="user_id" name="user_id" onchange="this.form.submit()">
              {user_options}
            </select>
          </div>
          <div class="form-group">
            <label for="workspace_id">Workspace <span class="text-muted">(optional)</span></label>
            <select id="workspace_id" name="workspace_id" onchange="this.form.submit()">
              <option value="">-- Optional --</option>
              {project_options}
            </select>
          </div>
        </div>
        <div class="form-row">
          <div class="form-group">
            <label for="agent_id">Agent</label>
            <select id="agent_id" name="agent_id" onchange="this.form.submit()">
              <option value="">-- Select agent --</option>
              {agent_options}
            </select>
          </div>
        </div>
      </div>

      {access_check_section}

      {verify_section_html}

      {destination_section}

      <div class="form-section" id="generated-output">
        <h2>Generated Output</h2>
        <div class="setup-tabs">{output_tabs}</div>
        <div class="alert alert-warning" id="connection-warning" style="display:none"></div>
        {output_label_html}
        {output_display}
        <div class="output-actions">{connection_btn}{copy_btn}{download_btn}{regenerate_btn}</div>
      </div>
    </form>

    <div class="setup-next-steps">
      <h3>Next Steps</h3>
      <ol>
        <li>Start with User Instructions if you are not sure which output to use.</li>
        <li>For most tools, MCP Config is the required connection step. Environment is optional and only stores values for shell or launcher use.</li>
        <li>Add AI-facing prompts or tool files only where the connected tool reads them.</li>
        <li>Run the Verification Prompt after setup to confirm connectivity.</li>
      </ol>
    </div>
    """

    return render_page("Integrations", body, "/agent-setup", _agent_setup_extra_js(), session=session)


def _agent_setup_extra_js():
    return """
<script>
function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, function(c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}

function downloadGeneratedOutput(filename, content) {
  const blob = new Blob([content], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}

function getGeneratedOutputText() {
  return document.querySelector('.output-block')?.innerText || '';
}

function copyGeneratedOutput(btn) {
  copyToClipboard(getGeneratedOutputText(), btn);
}

function downloadCurrentOutput(filename) {
  downloadGeneratedOutput(filename, getGeneratedOutputText());
}

function submitAgentSetupToOutput(input) {
  const form = input.form;
  if (!form) return;
  const params = new URLSearchParams(new FormData(form));
  window.location.href = form.getAttribute('action').split('#')[0] + '?' + params.toString() + '#generated-output';
}

async function applyRecommendedAccess() {
  const btn = document.getElementById('apply-access-btn');
  const status = document.getElementById('apply-status');
  const includeUserWrite = document.getElementById('include-user-write')?.checked ? true : false;
  btn.disabled = true;
  btn.textContent = 'Applying...';
  status.textContent = '';
  status.style.color = '';

  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value;
  const projectId = params.get('workspace_id') || document.getElementById('workspace_id')?.value;
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value;

  if (!userId || !projectId || !agentId) {
    status.textContent = 'Select user, workspace, and agent first.';
    status.style.color = 'var(--warning)';
    btn.disabled = false;
    btn.textContent = 'Apply Recommended Access';
    return;
  }

  try {
    const r = await fetch('/api/agent-setup/apply-recommended-access', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, workspace_id: projectId, agent_id: agentId, include_user_write: includeUserWrite })
    });
    const j = await r.json();
    if (j.ok) {
      status.textContent = 'Access updated. Reloading...';
      status.style.color = 'var(--success)';
      setTimeout(() => { window.location.href = window.location.pathname + '?user_id=' + encodeURIComponent(userId) + '&workspace_id=' + encodeURIComponent(projectId) + '&agent_id=' + encodeURIComponent(agentId) + '&output_type=' + (params.get('output_type') || 'instructions'); }, 800);
    } else {
      status.textContent = j.error?.message || 'Failed';
      status.style.color = 'var(--danger)';
      btn.disabled = false;
      btn.textContent = 'Apply Recommended Access';
    }
  } catch(e) {
    status.textContent = 'Error applying access';
    status.style.color = 'var(--danger)';
    btn.disabled = false;
    btn.textContent = 'Apply Recommended Access';
  }
}

async function generateConnectionConfig() {
  const btn = document.getElementById('generate-connection-btn');
  const warning = document.getElementById('connection-warning');
  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value;
  const projectId = params.get('workspace_id') || document.getElementById('workspace_id')?.value || '';
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value;
  const target = params.get('target') || 'generic_mcp';
  const outputType = params.get('output_type') || 'env';

  if (!userId || !agentId) {
    showToast('Select user and agent first', 'warning');
    return;
  }
  if (!confirm('Generate a new one-time key and config? This rotates the agent key and invalidates any previous key for this agent.')) return;

  btn.disabled = true;
  btn.textContent = 'Generating...';
  try {
    const r = await fetch('/api/agent-setup/generate-connection', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        user_id: userId,
        workspace_id: projectId,
        agent_id: agentId,
        target: target,
        output_type: outputType
      })
    });
    const j = await r.json();
    if (j.ok) {
      const block = document.querySelector('.output-block');
      if (block) block.innerText = j.data.output || '';
      const label = document.querySelector('.output-label');
      if (label) label.textContent = j.data.output_label || 'Connection Config';
      if (warning) {
        warning.textContent = j.data.warning || 'This key is shown once.';
        warning.style.display = 'block';
      }
      showToast('Connection config generated', 'success');
    } else {
      showToast(j.error?.message || 'Failed to generate config', 'danger');
    }
  } catch(e) {
    showToast('Failed to generate config', 'danger');
  }
  btn.disabled = false;
  btn.textContent = btn.dataset.label || 'Generate One-Time Key + Config';
}

async function runSetupVerification() {
  const btn = document.getElementById('verify-btn');
  const results = document.getElementById('verify-results');
  btn.disabled = true;
  btn.textContent = 'Checking...';
  results.innerHTML = '<div style="font-size:0.85rem;color:var(--muted)">Running setup checks...</div>';

  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value;
  const projectId = params.get('workspace_id') || document.getElementById('workspace_id')?.value;
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value;
  const writeTestMemory = document.getElementById('write-test-memory')?.checked ? true : false;

  if (!userId || !projectId || !agentId) {
    results.innerHTML = '<div style="color:var(--warning)">Select user, workspace, and agent first.</div>';
    btn.disabled = false;
    btn.textContent = 'Run Setup Check';
    return;
  }

  try {
    const r = await fetch('/api/agent-setup/verify', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ user_id: userId, workspace_id: projectId, agent_id: agentId, write_test_memory: writeTestMemory })
    });
    const j = await r.json();
    if (j.ok !== undefined) {
      let html = '<div style="margin-bottom:12px">';
      for (const check of (j.data?.checks || [])) {
        const cls = { ok: 'check-ok', error: 'check-blocked', warning: 'check-warn', blocked: 'check-blocked', skipped: 'check-info' }[check.status] || 'check-warn';
        const icon = { ok: '&#10003;', error: '&#10007;', warning: '&#9888;', blocked: '&#10007;', skipped: '&#8505;' }[check.status] || '&#9888;';
        html += '<div class="' + cls + '" style="margin-bottom:4px"><span class="check-icon">' + icon + '</span>' + escapeHtml(check.message) + '</div>';
      }
      html += '</div>';
      if (j.data?.ok) {
        html += '<div style="color:var(--success);font-size:0.85rem;margin-bottom:8px">All checks passed. Agent is connected and has workspace write access.</div>';
      } else {
        html += '<div style="color:var(--warning);font-size:0.85rem;margin-bottom:8px">Some checks failed. Review the errors above and consider applying recommended access.</div>';
      }
      results.innerHTML = html;
    } else {
      results.innerHTML = '<div style="color:var(--danger)">Verification failed: ' + (j.error?.message || 'Unknown error') + '</div>';
    }
  } catch(e) {
    results.innerHTML = '<div style="color:var(--danger)">Error running verification</div>';
  }
  btn.disabled = false;
  btn.textContent = 'Run Setup Check';
}
</script>
"""


def _build_agent_setup_output(
    user,
    workspace,
    agent,
    target,
    output_type,
    base_url,
    api_key=None,
):
    user_scope = f"user:{user['id']}"
    workspace_scope = f"workspace:{workspace['id']}" if workspace else ""
    default_scope = workspace_scope or user_scope
    agent_scope = f"agent:{agent['id']}"
    agent_display = agent.get("display_name", agent["id"])
    user_display = user.get("display_name", user.get("email", user["id"]))
    workspace_name = workspace.get("name", workspace["id"]) if workspace else "No workspace selected"

    if output_type == "instructions":
        label = "User Instructions"
        content = _build_user_instructions(target, base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name)
    elif output_type == "session":
        label = "Session Prompt"
        content = _build_session_prompt(target, base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name)
    elif output_type == "claude_md":
        label = "Workspace CLAUDE.md — paste into workspace repository root"
        content = _build_claude_md(base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name)
    elif output_type == "agents_md":
        label = "Workspace AGENTS.md — paste into workspace repository root"
        content = _build_agents_md(base_url, user_scope, workspace_scope, agent_scope, workspace_name)
    elif output_type == "mcp_json":
        label = "MCP Config"
        content = _build_mcp_json(base_url, api_key)
    elif output_type == "cursor_mcp_json":
        label = "MCP Config (Cursor)"
        content = _build_cursor_mcp_json(base_url, api_key)
    elif output_type == "windsurf_mcp_json":
        label = "MCP Config (Windsurf)"
        content = _build_windsurf_mcp_json(base_url, api_key)
    elif output_type == "env":
        label = "Environment Variables (optional key storage)"
        content = _build_env_vars(base_url, agent["id"], user_scope, workspace_scope, api_key)
    else:
        label = "Verification Prompt"
        content = _build_verification_prompt(default_scope)

    return label, content


def _build_instructions(target, base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name):
    scope_guide = ""
    tool_tips = ""
    default_scope = workspace_scope or user_scope
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    workspace_context_line = f"- Use `{workspace_scope}` for workspace facts, decisions, implementation notes, bugs, and architecture." if workspace_scope else f"- No workspace selected. Use `{user_scope}` for user-level context and `{agent_scope}` for private scratch context."
    if target == "claude_code":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Claude Code Tips

- Claude Code automatically reads `CLAUDE.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Claude Code will inherit the scopes from your Agent Core agent configuration. The scopes above are informational.
- For best results, set `AGENT_CORE_API_KEY` in your shell environment before starting Claude Code:
    export AGENT_CORE_API_KEY="your-key-here"
"""
    elif target == "codex":
        scope_guide = f"- Work in `{default_scope}` for default context.\n- Read `{user_scope}` for user preferences.\n- Keep private notes in `{agent_scope}`."
        tool_tips = f"""## Codex Tips

- Codex reads `AGENTS.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Codex can use both the MCP tools and the REST API. The MCP endpoint is preferred for memory operations.
- Set `AGENT_CORE_API_KEY` in your environment before starting a Codex session.
"""
    elif target == "cursor":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Cursor Tips

- Add the MCP config to `.cursor/mcp.json` in the workspace root for workspace-level access, or `~/.cursor/mcp.json` for global access.
- After adding the MCP config, run the "Reload MCP Servers" command or restart Cursor.
- Cursor's AI chat can use MCP tools directly once the server is connected. Set `AGENT_CORE_API_KEY` in Cursor's terminal or your shell profile.
"""
    elif target == "windsurf":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Windsurf Tips

- Add the MCP config to Windsurf's MCP settings for your workspace.
- Windsurf may require a restart after adding MCP servers.
- Set `AGENT_CORE_API_KEY` in your shell environment before starting a Windsurf session.
"""
    else:
        scope_guide = f"- Default memory scope: `{default_scope}`\n- User scope: `{user_scope}`\n- Private scope: `{agent_scope}`"
        tool_tips = f"""## Generic MCP Client Tips

- The MCP endpoint is `{base_url}/mcp`. Your client should send requests as JSON with `{{"tool": "...", "params": {{...}}}}`.
- Authenticate using `Authorization: Bearer $AGENT_CORE_API_KEY` header or your client's equivalent auth mechanism.
- Available tools include: `memory_search`, `memory_get`, `memory_write`, `memory_retract`, `vault_get`, `vault_list`, `activity_update`, `get_briefing`.
"""

    return f"""# Agent Core Setup Instructions

You are connected to Agent Core.

**User:** {user_display}
**Workspace:** {workspace_name}
**Agent:** {agent_display}
**Base URL:** {base_url}

## Scopes

{scope_guide}

## Getting Started

1. Search memory in `{default_scope}` for relevant context before starting work.
2. Search memory in `{user_scope}` for relevant user preferences.
3. Create or update an activity record when starting a meaningful task.
4. Store durable decisions and handoff notes in `{default_scope}`.
5. Use `vault_get` to retrieve credential references — never ask for raw secrets.

## Memory Write Rules

- Choose `decision` for durable choices and rationale.
- Choose `fact` for objective workspace state or implementation details.
- Choose `preference` for stable user or team preferences.
- Choose `scratchpad` only for temporary notes.
- Use `{workspace_scope_label}` for workspace memory when a workspace is selected, `{user_scope}` for stable user preferences, and `{agent_scope}` for private scratch context.
- Domain and topic are optional exact-match search filters. Add them only when they will help future retrieval.
- Confidence is caller-assigned and can be filtered by search; importance affects result ranking.

## API Key

Set `AGENT_CORE_API_KEY` in your local environment using the key shown when this agent was created or rotated.
Do not commit API keys to workspace files.

{tool_tips}
## Tool Configuration

- MCP endpoint: {base_url}/mcp
- REST base: {base_url}
- Auth: Bearer token with your agent API key
"""


def _build_user_instructions(target, base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name):
    workspace_line = (
        f"This setup is workspace-aware. Generated prompts use `{workspace_scope}` for workspace memory."
        if workspace_scope
        else f"No workspace is selected. Generated prompts use `{user_scope}` as the default shared context."
    )
    return f"""# Agent Core User Instructions

Use these steps to connect an AI tool to Agent Core as `{agent_display}` for {user_display}.

## What To Generate

1. Generate `MCP Config` when you are ready to connect the tool to Agent Core. This is the normal connection step for MCP-capable tools.
2. Generate `Environment Variables (optional key storage)` only when your MCP config or launcher reads values from environment variables.
3. Generate `Workspace CLAUDE.md` or `Workspace AGENTS.md` for reusable repository-level guidance shared by multiple agents.
4. Generate `Session Prompt` when you want one-time agent-specific instructions pasted into a chat/session.
5. Generate `Verification Prompt` after setup and paste it into the tool to confirm Agent Core connectivity.

## Connection Key

Click the one-time key button on `MCP Config` when you are ready to connect the tool.
Use the one-time key button on `Environment Variables (optional key storage)` only when you need shell or launcher environment variables.
That rotates this agent's API key and inserts the new key into the generated output.
The key is shown once. Generating again invalidates the previous key for `{agent_display}`.
The API key is the authoritative agent identity. Agent Core identifies requests as `{agent_scope}` by looking up the bearer token; repo instruction files do not set identity.

## Where Things Go

- MCP Config belongs in the MCP configuration location for the tool you are connecting. For Codex CLI, that is `~/.codex/config.toml`; for OpenCode, add the OpenCode block under `mcp` in `~/.config/opencode/opencode.json`.
- Environment variables belong in your shell profile, launcher, service environment, or tool-specific environment settings. They do not connect Agent Core by themselves.
- Session Prompt is pasted into the first message or custom instructions for a single session.
- `CLAUDE.md` and `AGENTS.md` belong in the workspace/repository root when you want persistent per-repository behavior. These files are workspace-centric and can be shared by Codex, OpenCode, Claude Code, and other agents using their own MCP keys.

## Selected Context

- Agent Core URL: `{base_url}`
- Connection agent for generated MCP/env output: `{agent_scope}`
- User: `{user_scope}`
- Workspace: `{workspace_scope or "optional / not selected"}`

Use the full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs like `{agent_display}` as memory scopes.

{workspace_line}
"""


def _build_session_prompt(target, base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name):
    default_scope = workspace_scope or user_scope
    tool_line = {
        "claude_code": "You are Claude Code.",
        "codex": "You are Codex.",
        "cursor": "You are Cursor's AI agent.",
        "windsurf": "You are Windsurf's AI agent.",
    }.get(target, "You are an MCP-capable AI agent.")
    return f"""{tool_line} You are working for {user_display} on {workspace_name}.

Use Agent Core MCP for durable workspace memory, handoffs, and workspace context.
Default memory scope for this setup is `{default_scope}`.
Use your private scope `{agent_scope}` only for tool-specific scratch context.
Use full prefixed scope names exactly as shown; do not use plain workspace IDs or agent IDs as memory scopes.
Read `{user_scope}` for stable {user_display} preferences when relevant.
Use vault references through Agent Core MCP; never request or print raw secrets.
Create or update an activity record when the task becomes meaningful.

When writing memory, choose the class deliberately: `decision` for durable choices, `fact` for objective context, `preference` for stable preferences, and `scratchpad` for temporary notes. Add domain/topic only when they will help future exact-match filtering. Use confidence for certainty and importance for retrieval ranking.

Start by confirming you can reach Agent Core at {base_url}/mcp, then search `{default_scope}` for relevant context before making changes.
"""


def _build_claude_md(base_url, user_scope, workspace_scope, agent_scope, agent_display, user_display, workspace_name):
    default_scope = workspace_scope or "the authenticated/default user scope from your Agent Core connection"
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    private_scope_guidance = (
        "Use your authenticated Agent Core private scope, usually `agent:<your-agent-id>`, only for tool-specific scratch context."
    )
    return f"""# Agent Core Workspace Context

You are working on the {workspace_name} workspace.

Use Agent Core for durable workspace memory, activity tracking, handoffs, and vault references.

## Connection

- **Agent Core URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active Agent Core user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not add API keys to this file.

## Memory Scopes

Use `{default_scope}` for default memory in this setup.
Read the authenticated/default user scope from your Agent Core connection only for stable personal preferences when relevant.
{private_scope_guidance}
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs like `{agent_display}` as memory scopes.

## Before Starting Work

1. Search memory in `{default_scope}` for relevant context.
2. Search the authenticated/default user scope for relevant personal preferences when needed.
3. Create or update an activity record for the current task.
4. Store durable decisions and handoff notes in `{default_scope}`.

## Writing Memory

When calling `memory_write`, choose:

- `decision` for durable choices and rationale.
- `fact` for objective workspace state or implementation details.
- `preference` for stable user or team preferences.
- `scratchpad` only for temporary notes.

Domain and topic are optional exact-match search filters. Confidence is caller-assigned and can be filtered by search. Importance affects search ranking.

## Credentials

Use `vault_get` to retrieve `AC_SECRET_*` references. The Credential Broker resolves them at execution time.
Never ask users for raw credential values.

## Activity Tracking

Send `activity_update` heartbeats every 1–2 minutes while working on a task.


## Claude Code Notes

- Claude Code automatically reads this `CLAUDE.md` file when present in the workspace root.
- Claude Code uses the configured MCP connection or your shell environment's `AGENT_CORE_API_KEY`. That key determines which Agent Core user and agent are active.
- Do not add your API key to this file.
- If Claude Code can't reach Agent Core, run the Verification Prompt output to verify connectivity.
"""


def _build_agents_md(base_url, user_scope, workspace_scope, agent_scope, workspace_name):
    default_scope = workspace_scope or "the authenticated/default user scope from your Agent Core connection"
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    return f"""# Agent Core Workspace Context

You are working on the {workspace_name} workspace.

## Agent Core

Use Agent Core MCP for memory, vault references, and activity tracking.

- **Base URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active Agent Core user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not commit credentials to this file.

## Memory Scope Guidance

Default memory scope for this setup is `{default_scope}`.
Read the authenticated/default user scope from your Agent Core connection for stable personal preferences when relevant.
Use your authenticated Agent Core private scope, usually `agent:<your-agent-id>`, for private scratch notes only.
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs or agent IDs as memory scopes.

## Before Starting

1. Confirm you can reach Agent Core at {base_url}/mcp.
2. Search `{default_scope}` for relevant context.
3. Write a short test memory to `{default_scope}` confirming setup.
4. Create an activity record for your current task.

## Writing Memory

Use `decision` for durable choices, `fact` for objective context, `preference` for stable preferences, and `scratchpad` only for temporary notes. Domain/topic are optional exact-match filters. Confidence can be filtered during search; importance affects ranking.

## Codex Notes

- Codex reads `AGENTS.md` at the start of each session.
- This file is workspace-centric and can be shared by multiple agents in the same repository. The MCP/API key determines whether the active agent is Codex, OpenCode, Claude Code, or another configured agent.
- For multi-agent collaboration, select a workspace and ensure each agent has read/write access to that workspace scope.
- Use the MCP tools (`memory_search`, `memory_write`) rather than raw API calls for better scope enforcement.
- If Codex loses connectivity, run the verification prompt to confirm the MCP server is reachable.
"""


def _connection_key_value(api_key=None):
    return api_key or "{{AGENT_CORE_API_KEY}}"


def _build_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    codex_auth = f'http_headers = {{ Authorization = "Bearer {key}" }}'
    generic_json = json.dumps({
        "mcpServers": {
            "agent-core": {
                "url": f"{base_url}/mcp",
                "headers": {
                    "Authorization": f"Bearer {key}",
                },
            },
        },
    }, indent=2)
    opencode_json = json.dumps({
        "mcp": {
            "agent-core": {
                "type": "remote",
                "url": f"{base_url}/mcp",
                "enabled": True,
                "headers": {
                    "Authorization": f"Bearer {key}",
                },
            },
        },
    }, indent=2)
    return f"""# Codex CLI: add this to ~/.codex/config.toml
[mcp_servers.agent-core]
url = "{base_url}/mcp"
{codex_auth}

# OpenCode: add this under ~/.config/opencode/opencode.json
{opencode_json}

# Generic MCP JSON clients:
{generic_json}
"""


def _build_cursor_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    return f"""{{
  "mcpServers": {{
    "agent-core": {{
      "url": "{base_url}/mcp",
      "headers": {{
        "Authorization": "Bearer {key}"
      }},
      "env": {{
        "AGENT_CORE_API_KEY": "{key}",
        "AGENT_CORE_URL": "{base_url}"
      }}
    }}
  }}
}}"""


def _build_windsurf_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    return f"""{{
  "mcpServers": {{
    "agent-core": {{
      "url": "{base_url}/mcp",
      "headers": {{
        "Authorization": "Bearer {key}"
      }},
      "env": {{
        "AGENT_CORE_API_KEY": "{key}",
        "AGENT_CORE_URL": "{base_url}"
      }}
    }}
  }}
}}"""


def _build_env_vars(base_url, agent_id, user_scope, workspace_scope, api_key=None):
    key = _connection_key_value(api_key)
    workspace_line = (
        f'export AGENT_CORE_WORKSPACE_SCOPE="{workspace_scope}"'
        if workspace_scope
        else '# export AGENT_CORE_WORKSPACE_SCOPE="workspace:your-workspace-id"  # Optional'
    )
    return f"""# Agent Core Environment Variables (optional key storage)
# Use these only when your MCP config, launcher, or script reads Agent Core values from the environment.
# MCP config is still the normal connection setup for MCP-capable tools.
# Do not commit these to workspace files.
#
# AGENT_CORE_API_KEY is the real authenticated agent identity.
# AGENT_CORE_AGENT_ID is helper metadata only; the server does not trust it for identity.

export AGENT_CORE_URL="{base_url}"
export AGENT_CORE_API_KEY="{key}"
export AGENT_CORE_AGENT_ID="{agent_id}"
export AGENT_CORE_USER_SCOPE="{user_scope}"
{workspace_line}
"""


def _get_destination_guidance(target, output_type):
    if output_type == "claude_md":
        return "Save this as <code>CLAUDE.md</code> in the workspace repository root. It is workspace-centric; the configured MCP/API key determines the active Agent Core agent."
    elif output_type == "agents_md":
        return "Save this as <code>AGENTS.md</code> in the workspace repository root. It is workspace-centric and can be shared by multiple agents; the configured MCP/API key determines the active Agent Core agent."
    elif output_type == "mcp_json":
        return "Add the matching block to your tool's MCP configuration. This is the normal connection artifact and the bearer key determines the active Agent Core agent."
    elif output_type == "env":
        return "Optional. Paste these into your shell profile (<code>~/.bashrc</code>, <code>~/.zshrc</code>) or tool environment only if you want the tool to read values from environment variables. This does not configure MCP by itself."
    elif output_type == "instructions":
        return "Read this yourself. It explains which generated output to use, where to put it, and when to generate a one-time key."
    elif output_type == "session":
        return "Paste this into the first message or workspace instructions field for a single agent session."
    elif output_type == "verification":
        return "Paste this into your connected agent after setup. It asks the agent to test Agent Core memory access and report the result."
    return ""


def _build_verification_prompt(workspace_scope):
    return f"""Check Agent Core connectivity:

1. Search memory in `{workspace_scope}` for "setup verification".
2. Write a memory record to `{workspace_scope}` saying this agent has been configured for the Agent Core workspace.
3. Report which scope you wrote to and why.

Use the full prefixed scope name exactly as shown. Do not use a plain workspace ID as a memory scope.
"""
