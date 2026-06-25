from fastapi import APIRouter, Request, Depends
from fastapi.responses import HTMLResponse
import httpx
import json
from urllib.parse import urlparse
from pydantic import BaseModel

from app.services.auth_service import get_user_by_id, count_users
from app.security.context import RequestContext
from app.security.dependencies import get_request_context
from app.security.dependencies import require_admin
from app.security.response_helpers import success_response, error_response
from app.security.scope_enforcer import ScopeEnforcer
from app.branding import APP_NAME, APP_SLUG, BACKUP_KEY_HEADER, CREDENTIAL_PREFIX, ENV_PREFIX, JS_WINDOW_EVENT  # noqa: F401 (BACKUP_KEY_HEADER used in f-string)
from app.routes.dashboard_shared import (
    escape_html,
    local_dt,
    get_icon,
    require_auth,
    render_page,
    _parse_manual_prune_cutoff,
    _query_in_values,
    _json_loads,
    _search_result,
)


router = APIRouter()


class DashboardSearchRequest(BaseModel):
    query: str
    limit: int = 5


class ManualPruneRequest(BaseModel):
    resource_type: str
    before_date: str




@router.post("/api/dashboard/search")
async def dashboard_search(
    body: DashboardSearchRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    from app.services import activity_service, connector_service, memory_service

    if ctx.actor_type != "user" and not ctx.is_admin:
        return error_response(
            "FORBIDDEN",
            "Dashboard search is only available to interactive dashboard sessions",
            403,
        )

    query = body.query.strip()
    if len(query) < 2:
        return error_response(
            "QUERY_TOO_SHORT", "Search query must be at least 2 characters", 400
        )

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    readable_scopes = enforcer.filter_readable_scopes(ctx.read_scopes)
    limit = max(1, min(int(body.limit or 5), 10))

    memory_hits: list[dict] = []
    if readable_scopes:
        memory_records, _ = memory_service.search_memory(
            query=query,
            authorized_scopes=readable_scopes,
            limit=limit,
            offset=0,
            include_retracted=False,
            include_superseded=False,
        )
        for record in memory_records:
            memory_hits.append(
                _search_result(
                    "memory",
                    record.get("content", "")[:120],
                    " · ".join(
                        part
                        for part in [
                            record.get("memory_class", ""),
                            record.get("scope", ""),
                            record.get("record_status", ""),
                        ]
                        if part
                    ),
                    "/memory",
                    "Memory",
                )
            )

    activity_rows = activity_service.list_activities(
        user_id=None if ctx.is_admin else ctx.user_id,
        limit=200,
    )
    activity_hits: list[dict] = []
    briefing_hits: list[dict] = []
    for activity in activity_rows:
        metadata = _json_loads(activity.get("metadata_json"))
        is_briefing = bool(
            isinstance(metadata, dict)
            and metadata.get("briefing")
            or (activity.get("task_description") or "").startswith("Briefing")
            or (activity.get("task_description") or "").startswith("Handoff briefing")
            or (activity.get("task_description") or "").startswith(
                "PRD briefing"
            )
            or (activity.get("task_description") or "").startswith(
                "PRD handoff briefing"
            )
        )
        search_blob = [
            activity.get("task_description", ""),
            activity.get("status", ""),
            activity.get("agent_id", ""),
            activity.get("assigned_agent_id", ""),
            activity.get("memory_scope", ""),
            activity.get("reassigned_from_agent_id", ""),
        ]
        if metadata:
            search_blob.append(metadata)
        if not _query_in_values(query, search_blob):
            continue

        summary_parts = [
            activity.get("status", ""),
            activity.get("agent_id", ""),
            activity.get("assigned_agent_id", ""),
            str(activity.get("updated_at") or activity.get("started_at") or "")[:16],
        ]
        summary = " · ".join(part for part in summary_parts if part)
        item = _search_result(
            "briefing" if is_briefing else "activity",
            activity.get("task_description", "")[:120],
            summary,
            "/activity",
            "Briefing" if is_briefing else "Activity",
        )
        if is_briefing:
            briefing_hits.append(item)
        else:
            activity_hits.append(item)
        if len(activity_hits) >= limit and len(briefing_hits) >= limit:
            break

    visible_bindings = connector_service.list_bindings()
    if not ctx.is_admin:
        visible_bindings = [b for b in visible_bindings if enforcer.can_read(b["scope"])]
    visible_connector_type_ids = {
        b["connector_type_id"] for b in visible_bindings
    } if not ctx.is_admin else set()

    connector_type_hits: list[dict] = []
    for connector_type in connector_service.list_connector_types():
        if not ctx.is_admin and connector_type["id"] not in visible_connector_type_ids:
            continue
        if not _query_in_values(
            query,
            connector_type.get("id", ""),
            connector_type.get("display_name", ""),
            connector_type.get("description", ""),
            connector_type.get("auth_type", ""),
            connector_type.get("supported_actions", []),
        ):
            continue
        connector_type_hits.append(
            _search_result(
                "connector",
                connector_type.get("display_name", connector_type.get("id", "")),
                " · ".join(
                    part
                    for part in [
                        connector_type.get("auth_type", ""),
                        f'{len(connector_type.get("supported_actions") or [])} actions',
                        f'{len(connector_type.get("disabled_actions") or [])} disabled',
                    ]
                    if part
                ),
                "/connectors",
                "Connector Type",
            )
        )
        if len(connector_type_hits) >= limit:
            break

    binding_hits: list[dict] = []
    for binding in visible_bindings:
        if not _query_in_values(
            query,
            binding.get("name", ""),
            binding.get("scope", ""),
            binding.get("connector_display_name", ""),
            binding.get("connector_type_id", ""),
            binding.get("last_error", ""),
            binding.get("config_json", ""),
        ):
            continue
        binding_hits.append(
            _search_result(
                "binding",
                binding.get("name", "")[:120],
                " · ".join(
                    part
                    for part in [
                        binding.get("connector_display_name", ""),
                        binding.get("scope", ""),
                        "enabled" if binding.get("enabled") else "disabled",
                    ]
                    if part
                ),
                "/connectors",
                "Binding",
            )
        )
        if len(binding_hits) >= limit:
            break

    return success_response(
        {
            "query": query,
            "counts": {
                "memory": len(memory_hits),
                "activities": len(activity_hits),
                "briefings": len(briefing_hits),
                "connector_types": len(connector_type_hits),
                "bindings": len(binding_hits),
            },
            "memory": memory_hits,
            "activities": activity_hits,
            "briefings": briefing_hits,
            "connector_types": connector_type_hits,
            "bindings": binding_hits,
        }
    )


@router.get("/")
async def dashboard_home(request: Request, session: dict = Depends(require_auth)):
    from app.services import activity_service
    from app.services import connector_service
    from app.services.agent_service import list_agents
    from app.services.workspace_service import list_workspaces
    from app.database import get_db

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"
    user_scope = f"user:{session['user_id']}"

    def count_rows(table: str, where: str = "1=1", params: tuple = ()) -> int:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", params
            ).fetchone()
            return int(row["count"] if row else 0)

    def count_connector_executions(where: str = "1=1", params: tuple = ()) -> int:
        with get_db() as conn:
            row = conn.execute(
                f"""
                SELECT COUNT(*) AS count
                FROM connector_executions ce
                JOIN connector_bindings cb ON ce.binding_id = cb.id
                WHERE {where}
                """,
                params,
            ).fetchone()
            return int(row["count"] if row else 0)

    if is_admin:
        agents = list_agents()
        workspaces = list_workspaces()
        connector_types = connector_service.list_connector_types()
        agent_count = len([a for a in agents if a.get("is_active")])
        workspace_count = len([p for p in workspaces if p.get("is_active")])
        memory_count = count_rows("memory_records", "record_status = 'active'")
        enabled_connector_binding_count = count_rows(
            "connector_bindings", "enabled = 1"
        )
        connector_execution_count = count_rows("connector_executions")
        recent_activity = activity_service.list_activities(limit=8)
        attention = activity_service.list_activities(
            status="stale", limit=8
        ) + activity_service.list_activities(status="blocked", limit=8)
    else:
        agents = list_agents(owner_user_id=session["user_id"])
        workspaces = list_workspaces(owner_user_id=session["user_id"])
        connector_types = connector_service.list_connector_types()
        agent_count = len([a for a in agents if a.get("is_active")])
        workspace_count = len([p for p in workspaces if p.get("is_active")])
        memory_count = count_rows(
            "memory_records", "scope = ? AND record_status = 'active'", (user_scope,)
        )
        enabled_connector_binding_count = count_rows(
            "connector_bindings", "scope = ? AND enabled = 1", (user_scope,)
        )
        connector_execution_count = count_connector_executions("cb.scope = ?", (user_scope,))
        recent_activity = activity_service.list_activities(
            user_id=session["user_id"], limit=8
        )
        attention = activity_service.list_activities(
            user_id=session["user_id"], status="stale", limit=8
        ) + activity_service.list_activities(
            user_id=session["user_id"], status="blocked", limit=8
        )

    active_task_count = count_rows(
        "agent_activity",
        "status IN ('active', 'reassigned')"
        if is_admin
        else "user_id = ? AND status IN ('active', 'reassigned')",
        () if is_admin else (session["user_id"],),
    )
    connector_type_count = len(connector_types)
    enabled_action_count = sum(
        max(len(ct.get("supported_actions") or []) - len(ct.get("disabled_actions") or []), 0)
        for ct in connector_types
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
      <a class="stat-card stat-link" href="/activity" id="stat-open-activities"><div class="value">{active_task_count}</div><div class="label">Open Activities</div></a>
      <a class="stat-card stat-link" href="/activity" id="stat-stale-blocked"><div class="value">{len(attention)}</div><div class="label">Stale / Blocked</div></a>
      <a class="stat-card stat-link" href="/memory"><div class="value">{memory_count}</div><div class="label">Memory Records</div></a>
    </div>"""

    capability_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Capability Snapshot</h3>
        <div class="section-note">What your agents can actually call right now.</div>
      </div>
      <div class="stat-grid">
        <a class="stat-card stat-link" href="/connectors"><div class="value">{connector_type_count}</div><div class="label">Connector Types</div></a>
        <a class="stat-card stat-link" href="/connectors"><div class="value">{enabled_connector_binding_count}</div><div class="label">Enabled Bindings</div></a>
        <a class="stat-card stat-link" href="/connectors"><div class="value">{enabled_action_count}</div><div class="label">Enabled Actions</div></a>
        <a class="stat-card stat-link" href="/connectors"><div class="value">{connector_execution_count}</div><div class="label">Connector Executions</div></a>
      </div>
    </div>"""

    search_html = """
    <div class="card">
      <div class="section-header">
        <h3>Operational Search</h3>
        <div class="section-note">Search memory, activities, briefings, connector types, and visible bindings.</div>
      </div>
      <form onsubmit="runDashboardSearch(event)" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input type="text" id="dashboard-search-query" class="search-input" placeholder="Search by task, memory, connector, or binding..." style="min-width:260px;flex:1" autocomplete="off">
        <button type="submit" class="btn">Search</button>
        <button type="button" class="btn btn-secondary" onclick="clearDashboardSearch()">Clear</button>
      </form>
      <p class="text-muted" style="margin:10px 0 0">This looks at the live operational state agents can actually use, then points you back to the right page.</p>
      <div id="dashboard-search-results" style="margin-top:14px">
        <div class="empty">Search operational state to find memory, work, briefings, and connector visibility.</div>
      </div>
    </div>"""

    attention_html = ""
    if attention:
        attention_rows = "".join(
            f"<tr><td>{a.get('task_description', '')[:60]}</td>"
            f"<td><span class='badge badge-{a.get('status', 'stale')}'>{a.get('status', '')}</span></td>"
            f"<td>{a.get('assigned_agent_id', '')}</td>"
            f"<td><a href='/activity'>Open</a></td></tr>"
            for a in attention
        )
        attention_html = f"""
    <div class="card attention-card">
      <h3>Needs Attention</h3>
      <table><thead><tr><th>Task</th><th>Status</th><th>Agent</th><th></th></tr></thead><tbody>{attention_rows}</tbody></table>
    </div>"""

    activity_rows = (
        "".join(
            f"<tr><td><div class='text-truncate' title='{escape_html(a.get('task_description', ''))}'>{escape_html(a.get('task_description', ''))}</div></td>"
            f"<td><span class='badge badge-{a.get('status', 'active')}'>{a.get('status', '')}</span></td>"
            f"<td>{escape_html(a.get('assigned_agent_id', ''))}</td>"
            f"<td>{local_dt(a.get('updated_at') or a.get('started_at'))}</td></tr>"
            for a in recent_activity[:6]
        )
        if recent_activity
        else "<tr><td colspan=4 class=empty>No recent activity.</td></tr>"
    )

    audit_link = (
        '<a href="/audit" class="btn btn-sm btn-secondary">Audit Log</a>'
        if is_admin
        else ""
    )

    return render_page(
        "Overview",
        f"""
    <div class="page-header">
      <h1>Overview</h1>
      <p class="text-muted" style="max-width:760px;margin-top:8px">
        {APP_NAME} is the local capability layer for your agents. This page gives you a quick read on
      memory, connectors, activity, and the services your agents can actually use.
      </p>
    </div>
    {stat_cards}
    {capability_html}
    {search_html}
    {attention_html}
    <div class="card">
      <div class="section-header">
        <h3>Recent Activity</h3>
        <div class="section-actions">
          <a href="/activity" class="btn btn-sm btn-secondary">View Activity</a>
          {audit_link}
        </div>
      </div>
      <table><thead><tr><th>Task</th><th>Status</th><th>Agent</th><th>Updated</th></tr></thead><tbody id="overview-activity-tbody">{activity_rows}</tbody></table>
    </div>
    <script>
    window.{JS_WINDOW_EVENT} = async function(event) {{
      if (!(event.type || '').startsWith('activity_')) return;
      var j = await apiFetch('/api/dashboard/activity/summary');
      if (!j.ok) return;
      var data = j.data || {{}};
      var openEl = document.getElementById('stat-open-activities');
      if (openEl) openEl.querySelector('.value').textContent = data.active_count || 0;
      var staleEl = document.getElementById('stat-stale-blocked');
      if (staleEl) staleEl.querySelector('.value').textContent = data.stale_count || 0;
      var tbody = document.getElementById('overview-activity-tbody');
      if (!tbody) return;
      var recent = (data.recent || []).slice(0, 6);
      if (!recent.length) {{
        tbody.innerHTML = '<tr><td colspan="4" class="empty">No recent activity.</td></tr>';
        return;
      }}
      tbody.innerHTML = recent.map(function(a) {{
        var ts = localDt(a.updated_at || a.started_at);
        return '<tr>' +
          '<td><div class="text-truncate" title="' + escapeHtml(a.task_description || '') + '">' + escapeHtml(a.task_description || '') + '</div></td>' +
          '<td><span class="badge badge-' + escapeHtml(a.status || '') + '">' + escapeHtml(a.status || '') + '</span></td>' +
          '<td>' + escapeHtml(a.assigned_agent_id || '') + '</td>' +
          '<td>' + escapeHtml(ts) + '</td>' +
        '</tr>';
      }}).join('');
    }};
    async function runDashboardSearch(e) {{
      if (e) e.preventDefault();
      const input = document.getElementById('dashboard-search-query');
      const results = document.getElementById('dashboard-search-results');
      const query = (input.value || '').trim();
      if (query.length < 2) {{
        results.innerHTML = '<div class="empty">Type at least 2 characters to search.</div>';
        return;
      }}
      results.innerHTML = '<div class="text-muted">Searching operational state...</div>';
      const j = await apiFetch('/api/dashboard/search', {{
        method: 'POST',
        body: JSON.stringify({{ query: query, limit: 5 }})
      }});
      if (!j.ok) {{
        results.innerHTML = '<div class="empty">Search failed.</div>';
        showToast(j.error?.message || 'Search failed', 'danger');
        return;
      }}
      const data = j.data || {{}};
      function renderItem(item) {{
        return '<div class="card" style="margin:8px 0;padding:12px">' +
          '<div style="display:flex;gap:12px;justify-content:space-between;align-items:flex-start">' +
            '<div style="min-width:0">' +
              '<div style="font-weight:600;word-break:break-word">' + escapeHtml(item.title || '') + '</div>' +
              '<div class="text-muted" style="margin-top:4px;font-size:0.9rem;word-break:break-word">' + escapeHtml(item.summary || '') + '</div>' +
              (item.meta ? '<div class="text-muted" style="margin-top:4px;font-size:0.85rem">' + escapeHtml(item.meta) + '</div>' : '') +
            '</div>' +
            '<a class="btn btn-sm btn-secondary" href="' + escapeHtml(item.href || '#') + '">Open</a>' +
          '</div>' +
        '</div>';
      }}
      function renderSection(title, items) {{
        if (!items || !items.length) return '';
        return '<div class="card" style="margin-top:12px;padding:12px">' +
          '<div class="section-header" style="margin-bottom:6px">' +
            '<h4 style="margin:0">' + escapeHtml(title) + '</h4>' +
            '<div class="section-note">' + items.length + ' match(es)</div>' +
          '</div>' +
          items.map(renderItem).join('') +
        '</div>';
      }}
      const sections = [
        renderSection('Memory', data.memory || []),
        renderSection('Briefings', data.briefings || []),
        renderSection('Activities', data.activities || []),
        renderSection('Connector Types', data.connector_types || []),
        renderSection('Bindings', data.bindings || [])
      ].filter(Boolean);
      results.innerHTML = sections.length
        ? sections.join('')
        : '<div class="empty">No matches found.</div>';
    }}
    function clearDashboardSearch() {{
      const input = document.getElementById('dashboard-search-query');
      const results = document.getElementById('dashboard-search-results');
      input.value = '';
      results.innerHTML = '<div class="empty">Search operational state to find memory, work, briefings, and connector visibility.</div>';
      input.focus();
    }}
    </script>
    """,
        "/",
        session=session,
    )


@router.get("/login")
async def login_page(request: Request):
    user_count = count_users()
    if user_count == 0:
        return render_page(
            "Setup",
            """
    <div class="card">
      <h3>Welcome to {APP_NAME}</h3>
      <p class="text-muted" style="margin-bottom:20px">Create your admin account to get started.</p>
      <form id="setup-form" onsubmit="submitSetup(event)">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" autocomplete="email" required>
        </div>
        <div class="form-group">
          <label>Display Name</label>
          <input type="text" name="display_name" autocomplete="name" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" minlength="8" autocomplete="new-password" required>
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
    """,
            "",
            show_sidebar=False,
        )
    return render_page(
        "Login",
        """
    <div class="card" style="max-width:400px;margin:60px auto">
      <h3>Sign In</h3>
      <form id="login-form" onsubmit="submitLogin(event)">
        <div class="form-group">
          <label>Email</label>
          <input type="email" name="email" autocomplete="email" required>
        </div>
        <div class="form-group">
          <label>Password</label>
          <input type="password" name="password" autocomplete="current-password" required>
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
    """,
        "",
        show_sidebar=False,
    )


@router.get("/otp")
async def otp_page(request: Request):
    return render_page(
        "OTP Verification",
        """
    <div class="card" style="max-width:400px;margin:60px auto">
      <h3>Two-Factor Authentication</h3>
      <p class="text-muted" style="margin-bottom:16px">Enter the 6-digit code from your authenticator app.</p>
      <form id="otp-form" onsubmit="submitOtp(event)">
        <div class="form-group">
          <input type="text" name="otp_code" placeholder="123456" autocomplete="one-time-code" style="width:260px;font-size:1rem;text-align:center">
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
    """,
        "",
        show_sidebar=False,
    )


@router.get("/logout")
async def logout_page(request: Request):
    return HTMLResponse("""<html><body>
<script>fetch('/api/auth/logout',{method:'POST'}).finally(()=>{window.location.href='/login'});</script>
<p>Logging out...</p></body></html>""")


# ─── SETTINGS ─────────────────────────────────────────────────────────────────


@router.post("/api/dashboard/user-settings")
async def update_dashboard_user_settings(
    request: Request, session: dict = Depends(require_auth)
):
    from app.services import auth_service

    body = await request.json()
    timezone = body.get("timezone")
    if timezone is not None:
        timezone = str(timezone).strip() or None
    try:
        auth_service.update_user_timezone(session["user_id"], timezone)
    except ValueError as e:
        return error_response("INVALID_TIMEZONE", str(e), 400)
    return success_response({"timezone": timezone})


@router.post("/api/dashboard/system-settings")
async def update_dashboard_system_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.database import get_db
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    retention_raw = str(body.get("scratchpad_retention_days", "")).strip()
    solo_raw = str(body.get("solo_mode_enabled", "")).strip().lower()

    try:
        retention_days = int(retention_raw)
    except ValueError:
        return error_response(
            "INVALID_RETENTION",
            "Scratchpad retention must be a whole number of days",
            400,
        )
    if retention_days < 1 or retention_days > 365:
        return error_response(
            "INVALID_RETENTION",
            "Scratchpad retention must be between 1 and 365 days",
            400,
        )
    if solo_raw not in ("true", "false"):
        return error_response(
            "INVALID_SOLO_MODE", "Solo mode must be true or false", 400
        )

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


@router.post("/api/dashboard/prune")
async def prune_dashboard_data(
    body: ManualPruneRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    from app.database import get_db
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    resource_type = body.resource_type.strip().lower()
    if resource_type not in {"audit", "activity"}:
        return error_response(
            "INVALID_RESOURCE",
            "resource_type must be either 'audit' or 'activity'",
            400,
        )

    try:
        cutoff_iso, cutoff_sql = _parse_manual_prune_cutoff(body.before_date)
    except ValueError:
        return error_response(
            "INVALID_DATE",
            "before_date must be a valid ISO date or datetime",
            400,
        )

    deleted = 0
    with get_db() as conn:
        if resource_type == "audit":
            cursor = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < ?",
                (cutoff_sql,),
            )
            deleted = cursor.rowcount
        else:
            cursor = conn.execute(
                """
                DELETE FROM agent_activity
                WHERE status IN ('completed', 'cancelled', 'blocked')
                  AND COALESCE(ended_at, updated_at, started_at) < ?
                """,
                (cutoff_iso,),
            )
            deleted = cursor.rowcount
        conn.commit()

    action = "audit_pruned" if resource_type == "audit" else "activity_pruned"
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action=action,
        resource_type=resource_type,
        result="success",
        details={
            "before_date": body.before_date,
            "cutoff": cutoff_iso,
            "deleted_count": deleted,
        },
        ip_address=get_client_ip(request),
    )
    return success_response(
        {
            "resource_type": resource_type,
            "before_date": body.before_date,
            "deleted_count": deleted,
        }
    )


@router.post("/api/dashboard/vector-settings")
async def update_dashboard_vector_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services.vector_settings_service import save_vector_settings
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    enabled_raw = body.get("vector_search_enabled")
    provider = body.get("vector_provider", "ollama").strip()
    auth_type = body.get("vector_auth_type", "none").strip()
    api_key = body.get("vector_api_key")
    model = body.get("vector_model", "").strip()
    url = body.get("vector_url", "").strip()
    dimension_raw = body.get("vector_dimension")

    if enabled_raw is not None and enabled_raw not in ("true", "false"):
        return error_response(
            "INVALID_ENABLED", "vector_search_enabled must be true or false", 400
        )

    if provider not in ("ollama", "generic"):
        return error_response(
            "INVALID_PROVIDER", "vector_provider must be ollama or generic", 400
        )

    if auth_type not in ("none", "bearer", "api_key"):
        return error_response(
            "INVALID_AUTH_TYPE",
            "vector_auth_type must be none, bearer, or api_key",
            400,
        )

    if not model:
        return error_response("INVALID_MODEL", "vector_model is required", 400)

    if not url:
        return error_response("INVALID_URL", "vector_url is required", 400)

    try:
        dimension = int(dimension_raw) if dimension_raw else None
        if dimension is not None and (dimension < 64 or dimension > 4096):
            return error_response(
                "INVALID_DIMENSION", "vector_dimension must be between 64 and 4096", 400
            )
    except ValueError:
        return error_response(
            "INVALID_DIMENSION", "vector_dimension must be a number", 400
        )

    updated = save_vector_settings(
        enabled=None if enabled_raw is None else (enabled_raw == "true"),
        provider=provider,
        url=url,
        model=model,
        dimension=dimension,
        auth_type=auth_type,
        api_key=api_key,
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="vector_settings",
        result="success",
        details={
            "vector_search_enabled": updated["vector_search_enabled"],
            "vector_auth_type": updated["vector_auth_type"],
            "vector_model": updated["vector_model"],
        },
        ip_address=get_client_ip(request),
    )

    return success_response({"settings": updated})


@router.post("/api/dashboard/vector-settings/test")
async def test_dashboard_vector_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services.vector_settings_service import test_vector_connection
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    result = test_vector_connection()
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="setup_verification",
        resource_type="vector_settings",
        result="success" if result.get("success") else "failure",
        details={k: v for k, v in result.items() if k != "error"},
        ip_address=get_client_ip(request),
    )
    if result.get("success"):
        return success_response(result)
    return error_response(
        "VECTOR_TEST_FAILED", result.get("error", "Vector test failed"), 400
    )


@router.get("/api/dashboard/vector-settings/models")
async def list_dashboard_vector_models(
    url: str, session: dict = Depends(require_admin)
):
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return error_response("INVALID_URL", "vector_url must be an http(s) URL", 400)

    tags_url = url.strip().rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(tags_url)
        if response.status_code != 200:
            return error_response(
                "MODEL_LIST_FAILED",
                f"Ollama returned {response.status_code} while listing models",
                502,
            )
        data = response.json()
    except Exception:
        return error_response(
            "MODEL_LIST_FAILED", "Could not fetch models from Ollama", 502
        )

    models = [
        model.get("name")
        for model in data.get("models", [])
        if isinstance(model, dict) and model.get("name")
    ]
    return success_response({"models": models})


@router.get("/settings")
async def settings_page(request: Request, session: dict = Depends(require_auth)):
    from app.database import get_db

    user = get_user_by_id(session["user_id"])
    is_admin = session.get("role") == "admin"

    def get_system_setting(key, default):
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
            return row["value"] if row else default

    def count_rows(table: str, where: str = "1=1", params: tuple = ()) -> int:
        with get_db() as conn:
            row = conn.execute(
                f"SELECT COUNT(*) AS count FROM {table} WHERE {where}", params
            ).fetchone()
            return int(row["count"] if row else 0)

    scratchpad_retention_days = get_system_setting("scratchpad_retention_days", "7")
    solo_mode_enabled = (
        get_system_setting("solo_mode_enabled", "true").lower() == "true"
    )
    credential_count = count_rows("credentials")

    account_html = f"""
    <div class="two-col">
      <div class="card">
        <h3>Account</h3>
        <p><strong>Display Name:</strong> {user.get("display_name", "")}</p>
        <p><strong>Email:</strong> {user.get("email", "")}</p>
        <p><strong>Role:</strong> {session.get("role", "user")}</p>
      </div>
      <div class="card">
        <h3>Security</h3>
        <div class="form-group">
          <a href="/settings/password" class="btn btn-secondary">Change Password</a>
        </div>
        <div class="form-group">
          <a href="/settings/otp" class="btn btn-secondary">Manage OTP</a>
        </div>
      </div>
    </div>"""

    from zoneinfo import available_timezones

    current_tz = user.get("timezone") or ""
    tz_option_list = '<option value="">Auto-detect from browser</option>'
    for _zone in sorted(available_timezones()):
        _sel = " selected" if _zone == current_tz else ""
        tz_option_list += (
            f'<option value="{escape_html(_zone)}"{_sel}>{escape_html(_zone)}</option>'
        )
    preferences_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Preferences</h3>
        <div class="section-note">Personal display options for your account.</div>
      </div>
      <form id="user-settings-form" onsubmit="saveUserSettings(event)">
        <div class="form-group">
          <label for="user-timezone">Timezone</label>
          <select id="user-timezone" style="max-width:360px">{tz_option_list}</select>
          <p class="form-hint">Dates and times across the dashboard are shown in this timezone. Stored data stays in UTC.</p>
        </div>
        <button type="submit" class="btn">Save Preferences</button>
        <div id="user-settings-result" style="margin-top:12px"></div>
      </form>
    </div>"""

    secrets_snapshot_html = ""
    if is_admin:
        from app.services.broker_service import get_broker_credential_hash
        from app.services.credential_rotation_service import get_key_status

        key_status = get_key_status()
        broker_active = bool(get_broker_credential_hash())
        broker_label = "present" if broker_active else "missing"
        broker_badge = "active" if broker_active else "stale"
        snapshot_rows = "".join(
            [
                f"<tr><td>Credential Entries</td><td>{credential_count}</td><td class='text-muted'>Encrypted secrets stored in the vault</td></tr>",
                f"<tr><td>Encryption Key</td><td><span class='badge badge-{ 'active' if key_status.get('mode') == 'keyring' else 'stale' }'>{escape_html(key_status.get('mode', 'unknown'))}</span></td><td class='text-muted'>Keyring size: {escape_html(key_status.get('keyring_size', 0))} · Primary key ID: {escape_html(key_status.get('primary_key_id', 'none'))}</td></tr>",
                f"<tr><td>Broker Credential</td><td><span class='badge badge-{broker_badge}'>{broker_label}</span></td><td class='text-muted'>Resolves {CREDENTIAL_PREFIX}* references at runtime</td></tr>",
            ]
        )
        secrets_snapshot_html = f"""
    <div class="card">
      <div class="section-header">
        <h3>Secrets Snapshot</h3>
        <div class="section-note">Current status for key and secret operations.</div>
      </div>
      <table>
        <thead><tr><th>Item</th><th>Status</th><th>Notes</th></tr></thead>
        <tbody>{snapshot_rows}</tbody>
      </table>
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
        <button class="btn" onclick="exportBackup()">Export Encrypted Backup</button>
        <button class="btn btn-secondary" onclick="openModal('restore-modal')">Restore from Backup</button>
        <button class="btn btn-secondary" onclick="runMaintenance()">Run Maintenance</button>
      </div>
      <p class="form-hint">Maintenance marks stale activities using <code>{ENV_PREFIX}STALE_THRESHOLD_MINUTES</code> and deletes transient scratchpad memories older than the retention setting below. This does not touch credentials or connector bindings.</p>
      <div id="backup-result" style="margin-top:12px"></div>
      <hr class=divider>
      <h4 class="section-title">Startup Checks</h4>
      <div id="startup-checks"><button class="btn btn-secondary btn-sm" onclick="runStartupChecks()">Run Checks</button></div>
    </div>"""

    encryption_key_html = ""
    if is_admin:
        encryption_key_html = """
    <div class="card">
      <h3>Encryption Key</h3>
      <p class="text-muted" style="margin-bottom:12px">This key protects stored credential values. Rotate it if you want to re-encrypt all saved secrets with a new primary key, or restore a known-good key if you need to recover from a bad rotation.</p>
      <div class="form-group">
        <button class="btn btn-secondary" onclick="loadCredentialKeyStatus()">Check Status</button>
        <button class="btn btn-danger" onclick="openModal('credential-key-rotate-modal')">Rotate Key</button>
        <button class="btn btn-secondary" onclick="openModal('credential-key-restore-modal')">Restore Key</button>
      </div>
      <div id="credential-key-result" style="margin-top:12px"></div>
    </div>"""

    broker_html = ""
    if is_admin:
        broker_html = """
    <div class="card">
      <h3>Broker</h3>
      <p class="text-muted" style="margin-bottom:12px">The broker credential resolves <code>{CREDENTIAL_PREFIX}*</code> references at runtime. Rotate it if a local consumer should stop using the current broker secret.</p>
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
          <p class="form-hint">Used by Run Maintenance. Transient scratchpad memories older than this many days are permanently deleted.</p>
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

    vector_settings_html = ""
    if is_admin:
        from app.services.vector_settings_service import get_vector_settings

        vector_settings = get_vector_settings()
        vector_enabled = vector_settings["vector_search_enabled"].lower() == "true"
        vector_provider = vector_settings.get("vector_provider", "ollama")
        vector_model = vector_settings["vector_model"]
        vector_url = vector_settings["vector_url"]
        vector_dimension = vector_settings["vector_dimension"]
        vector_auth_type = vector_settings["vector_auth_type"]
        vector_has_api_key = vector_settings["vector_has_api_key"]
        vector_checked = "checked" if vector_enabled else ""
        vector_api_key_hint = (
            "Leave blank to keep the stored key."
            if vector_has_api_key
            else "Optional. Stored encrypted when provided."
        )
        vector_settings_html = f"""
    <div class="card">
      <h3>Vector Search</h3>
      <p class="text-muted" style="margin-bottom:12px">Enable semantic search using an Ollama-compatible embedding endpoint. Save the settings to turn it on immediately when the backend is healthy. Disable to use FTS5 text search only.</p>
      <form id="vector-settings-form" onsubmit="saveVectorSettings(event)">
        <label class="checkbox-label" style="margin-bottom:16px">
          <input type="checkbox" id="vector-search-enabled" {vector_checked}>
          Enable vector semantic search
        </label>
        <div class="form-group">
          <label>Provider</label>
          <select id="vector-provider" style="width:200px" onchange="onProviderChange(this.value)">
            <option value="ollama" {"selected" if vector_provider == "ollama" else ""}>Ollama</option>
            <option value="generic" {"selected" if vector_provider == "generic" else ""}>Generic</option>
          </select>
        </div>
        <div class="form-group" id="vector-model-text-group">
          <label>Model</label>
          <input type="text" id="vector-model" value="{escape_html(vector_model)}" style="width:240px">
          <p class="form-hint" id="vector-model-hint-text">For Ollama, use <code>nomic-embed-text</code> or another installed embedding model.</p>
        </div>
        <div class="form-group" id="vector-model-select-group" style="display:none">
          <label>Model</label>
          <select id="vector-model-select" style="width:240px">
            <option value="">Loading models...</option>
          </select>
          <div style="margin-top:8px">
            <button type="button" class="btn btn-secondary btn-sm" onclick="loadOllamaModels()">Load Models</button>
          </div>
          <p class="form-hint" id="vector-model-hint-select"></p>
        </div>
        <div class="form-group">
          <label>URL</label>
          <input type="text" id="vector-url" value="{escape_html(vector_url)}" style="width:300px">
          <p class="form-hint">Base URL for an Ollama-compatible API, for example <code>http://localhost:11434</code>.</p>
        </div>
        <div class="form-group">
          <label>Embedding Dimension</label>
          <input type="number" id="vector-dimension" min="64" max="4096" value="{escape_html(vector_dimension)}" style="width:120px">
          <p class="form-hint">Must match the configured embedding model. <code>nomic-embed-text</code> uses 768.</p>
        </div>
        <div class="form-group">
          <label>Auth Type</label>
          <select id="vector-auth-type" style="width:200px">
            <option value="none" {"selected" if vector_auth_type == "none" else ""}>None</option>
            <option value="bearer" {"selected" if vector_auth_type == "bearer" else ""}>Bearer Token</option>
            <option value="api_key" {"selected" if vector_auth_type == "api_key" else ""}>API Key</option>
          </select>
        </div>
        <div class="form-group">
          <label>API Key</label>
          <input type="password" id="vector-api-key" value="" style="width:300px" autocomplete="off">
          <p class="form-hint">{escape_html(vector_api_key_hint)}</p>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn">Save Vector Settings</button>
          <button type="button" class="btn btn-secondary" onclick="testVectorSettings()">Test Connection</button>
        </div>
      </form>
      <div id="vector-settings-result" style="margin-top:12px"></div>
    </div>"""

    admin_modals = ""
    if is_admin:
        admin_modals = """
    <!-- Restore Modal -->
    <div class="modal-overlay" id="restore-modal" style="display:none">
      <div class="modal">
        <h3>Restore from Backup</h3>
        <div class="alert alert-danger">This can replace your current database and encryption key. Choose merge when you want to preserve current records.</div>
        <form id="restore-form" onsubmit="doRestore(event)">
          <div class="form-group">
            <label>Backup file *</label>
            <input type="file" id="restore-file" accept=".zip,.enc,.zip.enc" required>
          </div>
          <div class="form-group">
            <label>Backup Key</label>
            <input type="text" id="restore-backup-key" placeholder="Required for encrypted exports">
            <p class="form-hint">Encrypted backups need the one-time backup key shown at export time. Legacy unencrypted backups can still be restored without one.</p>
          </div>
          <div class="form-group">
            <label>Restore Mode *</label>
            <select id="restore-mode" required>
              <option value="replace_all">Replace all current data</option>
              <option value="merge">Merge non-conflicting records</option>
            </select>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="credential-key-rotate-modal" style="display:none">
      <div class="modal">
        <h3>Rotate Encryption Key</h3>
        <div class="alert alert-danger">This re-encrypts all credential entries with a new primary key. Keep backups of your database and keyring.</div>
        <form id="credential-key-rotate-form" onsubmit="rotateCredentialKey(event)">
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('credential-key-rotate-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Rotate Key</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="credential-key-restore-modal" style="display:none">
      <div class="modal">
        <h3>Restore Encryption Key</h3>
        <form id="credential-key-restore-form" onsubmit="restoreCredentialKey(event)">
          <div class="form-group">
            <label>Fernet Key *</label>
            <input type="text" id="credential-key-restore-key" required>
            <p class="form-hint">The key must decrypt all current credential entries before it is accepted.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('credential-key-restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore Key</button>
          </div>
        </form>
      </div>
    </div>"""

    js = """
    <script>
    async function saveUserSettings(e) {{
      e.preventDefault();
      const tz = document.getElementById('user-timezone').value;
      const j = await apiFetch('/api/dashboard/user-settings', {{ method: 'POST', body: JSON.stringify({{ timezone: tz }}) }});
      const res = document.getElementById('user-settings-result');
      if (j.ok) {{
        window.AC_USER_TZ = tz || (Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
        if (window.applyLocalTimes) window.applyLocalTimes(document);
        showToast('Preferences saved', 'success');
        res.innerHTML = '<div class="alert alert-success">Saved. Times now shown in ' + escapeHtml(window.AC_USER_TZ) + '.</div>';
      }} else {{
        res.innerHTML = '<div class="alert alert-danger">' + escapeHtml((j.error && j.error.message) || 'Save failed') + '</div>';
      }}
    }}
    window.saveUserSettings = saveUserSettings;

    async function exportBackup() {{
      const r = await fetch('/api/backup/export', {{
        method: 'POST',
      }});
      if (r.ok) {{
        const blob = await r.blob();
        const backupKey = r.headers.get('{BACKUP_KEY_HEADER}');
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = '{APP_SLUG}-backup.zip.enc'; a.click();
        URL.revokeObjectURL(url);
        const keyHtml = backupKey
          ? '<div class="alert alert-warning"><strong>Save this backup key now.</strong> It will not be shown again.<div class="api-key-display" style="margin-top:8px"><code>' + backupKey + '</code></div><button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(backupKey) + ', this)">Copy</button></div>'
          : '<div class="alert alert-warning"><strong>Backup key missing from response.</strong></div>';
        document.getElementById('backup-result').innerHTML = keyHtml;
        showToast('Encrypted backup downloaded');
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
          '<button class="copy-btn" onclick="copyToClipboard(' + JSON.stringify(j.data.credential) + ', this)">Copy</button>';
      }} else {{ showToast(j.error.message || 'Failed', 'danger'); }}
    }}
    async function loadCredentialKeyStatus() {{
      const j = await apiFetch('/api/credentials/rotate/status');
      if (j.ok) {{
        const s = j.data.key_status;
        document.getElementById('credential-key-result').innerHTML =
          '<div class="alert alert-success">Mode: <code>' + s.mode + '</code> · Keyring size: <code>' + s.keyring_size + '</code> · Primary key ID: <code>' + s.primary_key_id + '</code></div>';
      }} else {{ showToast(j.error?.message || 'Status failed', 'danger'); }}
    }}
    async function rotateCredentialKey(e) {{
      e.preventDefault();
      const j = await apiFetch('/api/credentials/rotate', {{
        method: 'POST'
      }});
      if (j.ok) {{
        closeModal('credential-key-rotate-modal');
        document.getElementById('credential-key-rotate-form').reset();
        document.getElementById('credential-key-result').innerHTML =
          '<div class="alert alert-success">' + j.data.message + ' Re-encrypted: <code>' + j.data.re_encrypted_count + '</code>. Keyring size: <code>' + j.data.keyring_size + '</code>.</div>';
      }} else {{ showToast(j.error?.message || 'Rotation failed', 'danger'); }}
    }}
    async function restoreCredentialKey(e) {{
      e.preventDefault();
      const key = document.getElementById('credential-key-restore-key').value.trim();
      if (!key) {{ showToast('Enter key first', 'warning'); return; }}
      const j = await apiFetch('/api/credentials/restore-key', {{
        method: 'POST',
        body: JSON.stringify({{ key_base64: key }})
      }});
      if (j.ok) {{
        closeModal('credential-key-restore-modal');
        document.getElementById('credential-key-restore-form').reset();
        document.getElementById('credential-key-result').innerHTML = '<div class="alert alert-success">' + j.data.message + '</div>';
      }} else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function doRestore(e) {{
      e.preventDefault();
      const formData = new FormData();
      const file = document.getElementById('restore-file').files[0];
      const backupKey = document.getElementById('restore-backup-key').value.trim();
      const mode = document.getElementById('restore-mode').value;
      if (!file) {{ showToast('Select a backup file', 'warning'); return; }}
      formData.append('backup', file);
      if (backupKey) formData.append('backup_key', backupKey);
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
    async function saveVectorSettings(e) {{
      e.preventDefault();
      const provider = document.getElementById('vector-provider').value;
      const selectedModel = document.getElementById('vector-model-select').value;
      const textModel = document.getElementById('vector-model').value;
      const model = provider === 'ollama' ? (selectedModel || textModel) : textModel;
      const body = {{
        vector_search_enabled: document.getElementById('vector-search-enabled').checked ? 'true' : 'false',
        vector_provider: provider,
        vector_model: model,
        vector_url: document.getElementById('vector-url').value,
        vector_dimension: document.getElementById('vector-dimension').value,
        vector_auth_type: document.getElementById('vector-auth-type').value,
      }};
      const apiKey = document.getElementById('vector-api-key').value;
      if (apiKey) body.vector_api_key = apiKey;
      const j = await apiFetch('/api/dashboard/vector-settings', {{
        method: 'POST',
        body: JSON.stringify(body)
      }});
      if (j.ok) {{
        document.getElementById('vector-api-key').value = '';
        document.getElementById('vector-model').value = model;
        document.getElementById('vector-settings-result').innerHTML = '<div class="alert alert-success">Vector settings saved.</div>';
      }} else {{
        showToast(j.error?.message || 'Failed to save vector settings', 'danger');
      }}
    }}
    async function testVectorSettings() {{
      const result = document.getElementById('vector-settings-result');
      result.innerHTML = '<div class="alert">Testing vector connection...</div>';
      const j = await apiFetch('/api/dashboard/vector-settings/test', {{ method: 'POST' }});
      if (j.ok) {{
        result.innerHTML = '<div class="alert alert-success">Vector connection succeeded for ' + escapeHtml(j.data.model || 'configured model') + '.</div>';
      }} else {{
        result.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Vector connection failed') + '</div>';
      }}
    }}
    async function onProviderChange(provider) {{
      const textGroup = document.getElementById('vector-model-text-group');
      const selectGroup = document.getElementById('vector-model-select-group');
      if (provider === 'ollama') {{
        textGroup.style.display = 'none';
        selectGroup.style.display = 'block';
        document.getElementById('vector-model-hint-select').textContent = 'Click Load Models after confirming the URL.';
      }} else {{
        textGroup.style.display = 'block';
        selectGroup.style.display = 'none';
      }}
    }}
    async function loadOllamaModels() {{
      const url = document.getElementById('vector-url').value.trim();
      if (!url) {{
        document.getElementById('vector-model-select').innerHTML = '<option value="">Enter URL first</option>';
        return;
      }}
      try {{
        const fullUrl = '/api/dashboard/vector-settings/models?url=' + encodeURIComponent(url);
        const resp = await fetch(fullUrl, {{ method: 'GET', credentials: 'same-origin' }});
        if (!resp.ok) throw new Error('Failed to fetch');
        const data = await resp.json();
        if (!data.ok) throw new Error(data.error?.message || 'Failed to fetch');
        const models = data.data?.models || [];
        if (models.length === 0) throw new Error('No models');
        const select = document.getElementById('vector-model-select');
        const currentModel = select.value || document.getElementById('vector-model').value || '';
        select.innerHTML = models.map(name => `<option value="${escapeHtml(name)}">${escapeHtml(name)}</option>`).join('');
        if (currentModel) select.value = currentModel;
        document.getElementById('vector-model-hint-select').textContent = 'Select an embedding model from Ollama';
      }} catch (e) {{
        document.getElementById('vector-model-select').innerHTML = '<option value="">Failed to load models</option>';
        document.getElementById('vector-model-hint-select').textContent = 'Could not fetch models from Ollama. Ensure Ollama is running.';
      }}
    }}
    function initVectorProvider() {{
      const provider = document.getElementById('vector-provider').value;
      if (provider === 'ollama') {{
        document.getElementById('vector-model-text-group').style.display = 'none';
        document.getElementById('vector-model-select-group').style.display = 'block';
        document.getElementById('vector-model-hint-select').textContent = 'Click Load Models after confirming the URL.';
      }}
    }}
    document.addEventListener('DOMContentLoaded', initVectorProvider);
    window.saveVectorSettings = saveVectorSettings;
    window.testVectorSettings = testVectorSettings;
    window.onProviderChange = onProviderChange;
    window.loadOllamaModels = loadOllamaModels;
    </script>"""
    js = js.replace("{{", "{").replace("}}", "}")

    return render_page(
        "Settings",
        f"""
    <div class="page-header"><h1>Settings</h1></div>
    {account_html}
    {preferences_html}
    {secrets_snapshot_html}
    {system_settings_html}
    {vector_settings_html}
    {encryption_key_html}
    {broker_html}
    {backup_html}
    {admin_modals}
    """,
        "/settings",
        js,
        session=session,
    )


# ─── SETTINGS PASSWORD ──────────────────────────────────────────────────────────


@router.get("/settings/password")
async def settings_password_page(
    request: Request, session: dict = Depends(require_auth)
):
    js = """
    <script>
    function showPasswordStatus(message, type) {
      const box = document.getElementById('password-status');
      if (!box) return;
      box.className = 'alert alert-' + (type || 'success');
      box.textContent = message;
      box.style.display = 'block';
    }
    async function submitPassword(e) {
      e.preventDefault();
      const status = document.getElementById('password-status');
      if (status) {
        status.style.display = 'none';
        status.textContent = '';
      }
      const current = document.getElementById('current-password').value;
      const new_pw = document.getElementById('new-password').value;
      const confirm = document.getElementById('confirm-password').value;
      if (new_pw !== confirm) { showPasswordStatus('New passwords do not match', 'warning'); return; }
      if (new_pw.length < 8) { showPasswordStatus('Password must be at least 8 characters', 'warning'); return; }
      const j = await apiFetch('/api/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: new_pw })
      });
      if (j.ok) {
        e.target.reset();
        showPasswordStatus('Password updated. You can continue this session or use the logout button below to sign back in with the new password.', 'success');
      } else {
        showPasswordStatus(j.error?.message || 'Failed', 'danger');
      }
    }
    </script>"""
    return render_page(
        "Change Password",
        """
    <div class="page-header"><h1>Change Password</h1></div>
    <div class="card" style="max-width:500px">
      <h3>Update Your Password</h3>
      <div id="password-status" class="alert alert-success" style="display:none"></div>
      <form id="password-form" onsubmit="submitPassword(event)">
        <div class="form-group">
          <label>Current Password</label>
          <input type="password" id="current-password" autocomplete="current-password" required>
        </div>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" id="new-password" minlength="8" autocomplete="new-password" required>
        </div>
        <div class="form-group">
          <label>Confirm New Password</label>
          <input type="password" id="confirm-password" minlength="8" autocomplete="new-password" required>
        </div>
        <div class="modal-footer" style="justify-content:flex-start;gap:8px;padding-left:0;padding-right:0">
          <button type="submit" class="btn">Update Password</button>
          <a class="btn btn-secondary" href="/logout">Log Out</a>
        </div>
      </form>
    </div>
    """,
        "/settings",
        js,
        session=session,
    )


# ─── SETTINGS OTP ───────────────────────────────────────────────────────────────


@router.get("/settings/otp")
async def settings_otp_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import is_otp_enrolled

    enrolled = is_otp_enrolled(session["user_id"])

    if enrolled:
        body_html = """
        <div class="alert alert-success">OTP is currently enrolled on your account.</div>
        <p class="text-muted">Resetting OTP requires your current password and an existing authenticator code. Disable OTP to turn it off completely and re-enroll later if you want.</p>
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
        <form id="otp-disable-form" onsubmit="submitOtpDisable(event)" style="margin-top:16px">
          <div class="form-group">
            <label for="otp-disable-password">Current Password</label>
            <input type="password" id="otp-disable-password" autocomplete="current-password" required>
          </div>
          <div class="form-group">
            <label for="otp-disable-code">Current OTP Code</label>
            <input type="text" id="otp-disable-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" required style="width:160px">
          </div>
          <button type="submit" class="btn btn-secondary">Disable OTP</button>
        </form>
        <div id="otp-reset-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function submitOtpReset(e) {
          e.preventDefault();
          if (!confirm('Reset OTP? Your existing authenticator setup will be replaced.')) return;
          await startOtpEnrollment('otp-reset-result');
        }
        async function submitOtpDisable(e) {
          e.preventDefault();
          if (!confirm('Disable OTP on this account?')) return;
          const password = document.getElementById('otp-disable-password')?.value || '';
          const otp = document.getElementById('otp-disable-code')?.value || '';
          if (!password || !otp) { showToast('Enter your current password and OTP code', 'warning'); return; }
          const j = await apiFetch('/api/auth/otp/disable', {
            method: 'POST',
            body: JSON.stringify({ current_password: password, otp_code: otp })
          });
          if (j.ok) {
            document.getElementById('otp-disable-form').reset();
            document.getElementById('otp-reset-result').innerHTML = '<div class="alert alert-success"><strong>OTP is disabled.</strong></div>';
          } else {
            showToast(j.error?.message || 'Failed', 'danger');
          }
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
              '<div class="alert alert-success"><strong>OTP is enabled.</strong></div>';
          } else {
            showToast(j.error?.message || 'Invalid code', 'danger');
          }
        }
        </script>
    """
    js = setup_js + js

    return render_page(
        "Manage OTP",
        f"""
    <div class="page-header"><h1>Two-Factor Authentication</h1></div>
    <div class="card" style="max-width:600px">
      {body_html}
    </div>
    """,
        "/settings",
        js,
        session=session,
    )


# ─── INTEGRATIONS ─────────────────────────────────────────────────────────────


@router.post("/api/integrations/apply-recommended-access")
@router.post("/api/integrations/apply-access")
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

    can_read_workspace = is_admin or workspace_service.can_user_read_workspace(
        current_user_id, body.workspace_id
    )
    can_write_workspace = is_admin or workspace_service.can_user_write_workspace(
        current_user_id, body.workspace_id
    )

    if not can_read_workspace:
        return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)

    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response(
            "AGENT_USER_MISMATCH",
            "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.",
            400,
        )

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
    if can_read_workspace:
        add_scope(read_scopes, workspace_scope)
    if can_write_workspace:
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

    return success_response(
        {
            "message": "Access updated",
            "read_scopes": read_scopes,
            "write_scopes": write_scopes,
        }
    )


def _agent_context_user_id(agent):
    return agent.get("default_user_id") or agent.get("owner_user_id")


def _agent_user_matches(agent, user_id):
    return _agent_context_user_id(agent) == user_id


def _agent_setup_output_options(target=None):
    return [
        ("instructions", "Instructions", f"{APP_SLUG}-instructions.md"),
        ("mcp_json", "MCP Config", f"{APP_SLUG}-mcp-config.txt"),
        ("env", "Environment Variables", f"{APP_SLUG}.env"),
        ("claude_md", "CLAUDE.md", "CLAUDE.md"),
        ("agents_md", "AGENTS.md", "AGENTS.md"),
        ("assistants_md", "Assistants", "Assistants"),
        ("session", "Session Prompt", f"{APP_SLUG}-session-prompt.md"),
        ("verification", "Verification Prompt", f"{APP_SLUG}-verification.md"),
    ]


def _agent_setup_target_label(target):
    return {
        "claude_code": "Claude Code",
        "codex": "Codex",
        "cursor": "Cursor",
        "windsurf": "Windsurf",
        "antigravity": "Antigravity",
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
    workspace_can_read = False
    workspace_can_write = False
    if workspace:
        workspace_can_read = workspace_service.can_user_read_workspace(user_id, workspace["id"])
        workspace_can_write = workspace_service.can_user_write_workspace(user_id, workspace["id"])
    enforcer = ScopeEnforcer(
        read_scopes,
        write_scopes,
        agent_id,
        active_workspace_ids=workspace_service.get_active_workspace_ids(workspace_ids),
    )
    checks = []

    checks.append(
        {
            "label": "Agent active",
            "status": "ok" if agent.get("is_active") else "blocked",
        }
    )
    if workspace:
        if workspace.get("is_active"):
            checks.append({"label": "Workspace active", "status": "ok"})
        else:
            checks.append({"label": "Workspace inactive", "status": "warning"})

        can_read_workspace = enforcer.can_read(workspace_scope) and workspace_can_read
        can_write_workspace = enforcer.can_write(workspace_scope) and workspace_can_write
        if can_read_workspace and can_write_workspace:
            checks.append({"label": "Workspace read/write access", "status": "ok"})
        elif can_read_workspace:
            checks.append({"label": "Workspace read-only access", "status": "warning"})
        else:
            checks.append({"label": "No workspace access", "status": "blocked"})
            checks.append(
                {
                    "label": "Recommended: add workspace scope to agent",
                    "status": "warning",
                }
            )
    else:
        checks.append({"label": "No workspace selected", "status": "info"})

    can_read_user = enforcer.can_read(user_scope)
    can_write_user = enforcer.can_write(user_scope)
    if can_read_user and can_write_user:
        checks.append(
            {"label": "User preference read/write access", "status": "warning"}
        )
        checks.append(
            {"label": "Warning: user-scope write access granted", "status": "warning"}
        )
    elif can_read_user:
        checks.append({"label": "User preference read access", "status": "ok"})
    else:
        checks.append({"label": "No user preference access", "status": "warning"})
        checks.append(
            {"label": "Recommended: add user scope to agent", "status": "warning"}
        )

    if enforcer.can_read(agent_scope) and enforcer.can_write(agent_scope):
        checks.append({"label": "Agent private scope", "status": "ok"})
    else:
        checks.append({"label": "Agent private scope incomplete", "status": "warning"})

    if workspace_scope and enforcer.can_read(workspace_scope):
        checks.append({"label": "Credential access (workspace scope)", "status": "ok"})
    elif enforcer.can_read("shared") or enforcer.can_read(user_scope):
        checks.append(
            {"label": "Credential access (user/shared scope)", "status": "ok"}
        )
    else:
        checks.append({"label": "Credential access", "status": "warning"})

    if agent.get("owner_user_id") == user_id or is_admin:
        checks.append({"label": "Activity tracking", "status": "ok"})
    else:
        checks.append({"label": "Activity tracking (limited)", "status": "warning"})

    checks.append(
        {
            "label": "Scope model: workspace membership gates agent scopes",
            "status": "info",
        }
    )
    recommended_read = read_scopes + [agent_scope, user_scope]
    recommended_write = write_scopes + [agent_scope]
    if workspace_scope and workspace_can_read:
        recommended_read.append(workspace_scope)
    if workspace_scope and workspace_can_write:
        recommended_write.append(workspace_scope)
    recommended = {
        "read": sorted(set(recommended_read)),
        "write": sorted(set(recommended_write)),
    }
    return checks, recommended


@router.post("/api/integrations/preview")
async def preview_agent_setup(
    request: Request,
    session: dict = Depends(require_auth),
):
    from pydantic import BaseModel
    from app.services.agent_service import list_agents
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
        if not is_admin and not workspace_service.can_user_read_workspace(current_user_id, body.workspace_id):
            return error_response("FORBIDDEN", "Access denied", 403)

    agents = list_agents() if is_admin else list_agents(owner_user_id=current_user_id)
    agent = next(
        (a for a in agents if a["id"] == body.agent_id and a.get("is_active")), None
    )
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not _agent_user_matches(agent, body.user_id):
        return error_response(
            "AGENT_USER_MISMATCH",
            "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.",
            400,
        )

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

    return success_response(
        {
            "recommended_scopes": recommended,
            "access_checks": access_checks,
            "outputs": outputs,
            "selected_output": outputs.get(
                body.output_type, outputs.get("instructions", "")
            ),
        }
    )


@router.post("/api/integrations/generate-connection")
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
        if not is_admin and not workspace_service.can_user_read_workspace(current_user_id, body.workspace_id):
            return error_response("FORBIDDEN", "Access denied", 403)

    agent = agent_service.get_agent_by_id(body.agent_id)
    if not agent:
        return error_response("NOT_FOUND", "Agent not found", 404)
    if not agent.get("is_active"):
        return error_response(
            "AGENT_INACTIVE", "Cannot generate config for inactive agent", 400
        )
    if not is_admin and agent.get("owner_user_id") != current_user_id:
        return error_response("FORBIDDEN", "Access denied", 403)
    if not _agent_user_matches(agent, body.user_id):
        return error_response(
            "AGENT_USER_MISMATCH",
            "Agents are tied to one owner/default user. Create a separate agent for this user and share workspace access through workspace scopes.",
            400,
        )

    api_key = agent_service.rotate_agent_key(agent["id"])
    if not api_key:
        return error_response(
            "AGENT_INACTIVE", "Cannot rotate key for inactive agent", 400
        )

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

    filename = next(
        (
            f
            for v, _l, f in _agent_setup_output_options(body.target)
            if v == body.output_type
        ),
        f"{APP_SLUG}-output.txt",
    )
    return success_response(
        {
            "agent_id": agent["id"],
            "output_type": body.output_type,
            "output_label": output_label,
            "filename": filename,
            "output": output,
            "api_key": api_key,
            "warning": "This key is shown once. Generating again rotates the agent key and invalidates the previous key.",
        }
    )


@router.get("/integrations")
async def integrations_page(
    request: Request,
    session: dict = Depends(require_auth),
):
    from app.services.agent_service import list_agents
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
            rows = conn.execute(
                "SELECT id, email, display_name FROM users ORDER BY display_name"
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, email, display_name FROM users WHERE id = ?",
                (current_user_id,),
            ).fetchall()
    users = [
        {"id": r["id"], "email": r["email"], "display_name": r["display_name"]}
        for r in rows
    ]

    workspaces = (
        workspace_service.list_workspaces()
        if is_admin
        else workspace_service.list_workspaces(owner_user_id=current_user_id)
    )
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

    access_checks = []
    generated_output = ""
    output_label = ""
    base_url = str(request.base_url).rstrip("/")
    page_path = request.url.path
    recommended_scopes = None

    if agent_id:
        agent = visible_agents.get(agent_id)
        workspace = (
            next((p for p in workspaces if p["id"] == workspace_id), None)
            if workspace_id
            else None
        )
        user = next((u for u in users if u["id"] == user_id), None)

        if agent and agent.get("is_active"):
            access_checks.append({"label": "Agent active", "status": "ok"})
        elif agent:
            access_checks.append({"label": "Agent inactive", "status": "blocked"})
        else:
            access_checks.append({"label": "Agent not found", "status": "blocked"})

        if not workspace_id:
            access_checks.append(
                {"label": "Workspace optional for this output", "status": "info"}
            )
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
    if access_checks:
        checks_html = "<div class='access-checks'>"
        for check in access_checks:
            cls = {
                "ok": "check-ok",
                "warning": "check-warn",
                "blocked": "check-blocked",
                "info": "check-info",
            }[check["status"]]
            icon = {
                "ok": "&#10003;",
                "warning": "&#9888;",
                "blocked": "&#10007;",
                "info": "&#8505;",
            }[check["status"]]
            checks_html += f"<div class='{cls}'><span class='check-icon'>{icon}</span>{escape_html(check['label'])}</div>"
        checks_html += "</div>"
        if any(c["status"] == "warning" for c in access_checks):
            checks_html += "<p class='text-muted' style='font-size:0.8rem;margin-top:8px'>Warnings indicate missing access. Review access on the Agents page or generate the prompt anyway.</p>"

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
        output_tabs += f'<a class="setup-tab {active}" href="{page_path}?{urlencode(params)}">{escape_html(label)}</a>\n'
    filename = next(
        (f for v, _l, f in _agent_setup_output_options(target) if v == output_type),
        f"{APP_SLUG}-output.txt",
    )

    if generated_output:
        output_display = (
            f"<pre class='output-block'>{escape_html(generated_output)}</pre>"
        )
        copy_btn = "<button type='button' class='btn btn-sm btn-secondary' onclick=\"copyGeneratedOutput(this)\">Copy</button>"
        download_btn = f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"downloadCurrentOutput('{escape_html(filename)}')\">Download</button>"
        regenerate_btn = (
            "<button type='submit' class='btn btn-sm btn-secondary'>Regenerate</button>"
        )
        connection_label = (
            "Generate One-Time Key + MCP Config"
            if output_type == "mcp_json"
            else "Generate One-Time Key + Environment Variables"
            if output_type == "env"
            else "Generate One-Time Key + Assistants Prompt"
        )
        connection_btn = (
            f"<button type='button' class='btn btn-sm btn-warning' id='generate-connection-btn' data-label='{escape_html(connection_label)}' onclick='generateConnectionConfig()'>{escape_html(connection_label)}</button>"
            if output_type in ("env", "mcp_json", "assistants_md")
            else ""
        )
    else:
        output_display = "<div class='empty'>Select a user and agent to generate setup output. Select a workspace only when you want workspace-specific memory guidance.</div>"
        copy_btn = ""
        download_btn = ""
        regenerate_btn = ""
        connection_btn = ""

    destination_guidance = _get_destination_guidance(target, output_type)

    access_check_section = (
        f'<div class="form-section"><h2>Access Check</h2>{checks_html}</div>'
        if checks_html
        else ""
    )
    destination_section = (
        f'<div class="form-section"><h2>Destination</h2><p>{destination_guidance}</p></div>'
        if destination_guidance
        else ""
    )
    output_label_html = (
        f"<div class='output-label'>{output_label}</div>" if output_label else ""
    )
    body = f"""
    <div class="page-header setup-page-header">
      <h1>Integrations</h1>
      <p class="subtitle">Generate setup instructions, environment variables, MCP config, and AI-facing prompts for connecting tools to {APP_NAME}.</p>
      <div class="text-muted" style="font-size:0.86rem;margin-top:8px">
        Current tool preset: <strong>{escape_html(tool_label)}</strong>. The main presets are Claude Code, Codex, Cursor, Windsurf, Antigravity, and Generic MCP/REST. The generated outputs in this page are the canonical setup files and prompts.
      </div>
    </div>

    <form method="get" action="{page_path}" class="setup-form" onsubmit="saveSetupScrollPosition()">
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
        <li>Start with Instructions if you are not sure which output to use.</li>
        <li>For most tools, MCP Config is the required connection step. Environment is optional and only stores values for shell or launcher use.</li>
        <li>Add CLAUDE.md, AGENTS.md, or Session Prompt only where the connected tool reads them.</li>
        <li>Run the Verification Prompt in the connected agent to confirm end-to-end memory, credential, and connector access.</li>
      </ol>
    </div>
    """

    return render_page(
        "Integrations", body, page_path, _agent_setup_extra_js(), session=session
    )


def _agent_setup_extra_js():
    _bracket = "{{" + ENV_PREFIX + "API_KEY}}"
    _angle = "<" + ENV_PREFIX + "API_KEY>"
    _js_constants = f"\nconst _AC_APP_SLUG = '{APP_SLUG}';\nconst _AC_API_KEY_BRACKET = '{_bracket}';\nconst _AC_API_KEY_ANGLE = '{_angle}';\n"
    return "\n<script>" + _js_constants + """
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

function getSetupScrollStorageKey() {
  return (_AC_APP_SLUG + '-integrations-scroll:') + window.location.pathname;
}

function saveSetupScrollPosition() {
  try {
    sessionStorage.setItem(getSetupScrollStorageKey(), String(window.scrollY || 0));
  } catch (e) {}
}

function restoreSetupScrollPosition() {
  try {
    const raw = sessionStorage.getItem(getSetupScrollStorageKey());
    if (raw === null) return;
    const y = parseInt(raw, 10);
    if (!Number.isFinite(y)) return;
    requestAnimationFrame(() => window.scrollTo({ top: y, behavior: 'auto' }));
  } catch (e) {}
}

function getIntegrationConnectionKeyStorageKey() {
  const params = new URLSearchParams(window.location.search);
  const userId = params.get('user_id') || document.getElementById('user_id')?.value || '';
  const workspaceId = params.get('workspace_id') || document.getElementById('workspace_id')?.value || '';
  const agentId = params.get('agent_id') || document.getElementById('agent_id')?.value || '';
  const target = params.get('target') || 'generic_mcp';
  return [_AC_APP_SLUG + '-connection-key', userId, workspaceId, agentId, target].join(':');
}

function getStoredIntegrationConnectionKey() {
  try {
    return sessionStorage.getItem(getIntegrationConnectionKeyStorageKey()) || '';
  } catch (e) {
    return '';
  }
}

function setStoredIntegrationConnectionKey(key) {
  try {
    if (key) sessionStorage.setItem(getIntegrationConnectionKeyStorageKey(), key);
  } catch (e) {}
}

function applyStoredIntegrationConnectionKey() {
  const key = getStoredIntegrationConnectionKey();
  if (!key) return;
  const block = document.querySelector('.output-block');
  if (!block) return;
  const current = block.innerText || '';
  if (!current.includes(_AC_API_KEY_ANGLE) && !current.includes(_AC_API_KEY_BRACKET)) return;
  const updated = current
    .replaceAll(_AC_API_KEY_ANGLE, key)
    .replaceAll(_AC_API_KEY_BRACKET, key);
  if (updated !== current) {
    block.innerText = updated;
    const warning = document.getElementById('connection-warning');
    if (warning) {
      warning.textContent = 'This page is using the last generated one-time key for the current context.';
      warning.style.display = 'block';
    }
  }
}

function copyGeneratedOutput(btn) {
  copyToClipboard(getGeneratedOutputText(), btn);
}

function downloadCurrentOutput(filename) {
  downloadGeneratedOutput(filename, getGeneratedOutputText());
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
    const r = await fetch('/api/integrations/apply-recommended-access', {
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
    const r = await fetch('/api/integrations/generate-connection', {
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
      setStoredIntegrationConnectionKey(j.data.api_key || '');
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

document.addEventListener('DOMContentLoaded', applyStoredIntegrationConnectionKey);
document.addEventListener('DOMContentLoaded', restoreSetupScrollPosition);
window.addEventListener('beforeunload', saveSetupScrollPosition);

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
    workspace_name = (
        workspace.get("name", workspace["id"]) if workspace else "No workspace selected"
    )

    if output_type == "instructions":
        label = "Instructions"
        content = _build_user_instructions(
            target,
            base_url,
            user_scope,
            workspace_scope,
            agent_scope,
            agent_display,
            user_display,
            workspace_name,
        )
    elif output_type == "session":
        label = "Session Prompt"
        content = _build_session_prompt(
            target,
            base_url,
            user_scope,
            workspace_scope,
            agent_scope,
            agent_display,
            user_display,
            workspace_name,
        )
    elif output_type == "claude_md":
        label = "CLAUDE.md — paste into workspace repository root"
        content = _build_claude_md(
            base_url,
            user_scope,
            workspace_scope,
            agent_scope,
            agent_display,
            workspace_name,
        )
    elif output_type == "agents_md":
        label = "AGENTS.md — paste into workspace repository root"
        content = _build_agents_md(
            base_url, user_scope, workspace_scope, agent_scope, workspace_name
        )
    elif output_type == "assistants_md":
        label = "Assistants — paste into the assistant's onboarding or instruction field"
        content = _build_assistants_md(
            base_url, user_scope, workspace_scope, agent_scope, api_key=api_key,
            default_recall_scopes_json=agent.get("default_recall_scopes_json"),
        )
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
        label = "Environment Variables"
        content = _build_env_vars(
            base_url, agent["id"], user_scope, workspace_scope, api_key
        )
    else:
        label = "Verification Prompt"
        content = _build_verification_prompt(user_scope, default_scope)

    return label, content


def _build_instructions(
    target,
    base_url,
    user_scope,
    workspace_scope,
    agent_scope,
    agent_display,
    user_display,
    workspace_name,
):
    scope_guide = ""
    tool_tips = ""
    default_scope = workspace_scope or user_scope
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    workspace_context_line = (
        f"- Use `{workspace_scope}` for workspace facts, decisions, implementation notes, bugs, and architecture."
        if workspace_scope
        else f"- No workspace selected. Use `{user_scope}` for user-level context and `{agent_scope}` for private scratch context."
    )
    if target == "claude_code":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Claude Code Tips

- Claude Code automatically reads `CLAUDE.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Claude Code will inherit the scopes from your {APP_NAME} agent configuration. The scopes above are informational.
- For best results, set `{ENV_PREFIX}API_KEY` in your shell environment before starting Claude Code:
    export {ENV_PREFIX}API_KEY="your-key-here"
"""
    elif target == "codex":
        scope_guide = f"- Work in `{default_scope}` for default context.\n- Read `{user_scope}` for user preferences.\n- Keep private notes in `{agent_scope}`."
        tool_tips = f"""## Codex Tips

- Codex reads `AGENTS.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Codex can use both the MCP tools and the REST API. The MCP endpoint is preferred for memory operations.
- Set `{ENV_PREFIX}API_KEY` in your environment before starting a Codex session.
"""
    elif target == "cursor":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Cursor Tips

- Add the MCP config to `.cursor/mcp.json` in the workspace root for workspace-level access, or `~/.cursor/mcp.json` for global access.
- After adding the MCP config, run the "Reload MCP Servers" command or restart Cursor.
- Cursor's AI chat can use MCP tools directly once the server is connected. Set `{ENV_PREFIX}API_KEY` in Cursor's terminal or your shell profile.
"""
    elif target == "windsurf":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Windsurf Tips

- Add the MCP config to Windsurf's MCP settings for your workspace.
- Windsurf may require a restart after adding MCP servers.
- Set `{ENV_PREFIX}API_KEY` in your shell environment before starting a Windsurf session.
"""
    elif target == "antigravity":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = f"""## Antigravity Tips

- Add the MCP config using the `serverUrl` field instead of `url`.
- Restart Antigravity after adding MCP servers if the config does not appear immediately.
- Set `{ENV_PREFIX}API_KEY` in your shell environment before starting an Antigravity session.
"""
    else:
        scope_guide = f"- Default memory scope: `{default_scope}`\n- User scope: `{user_scope}`\n- Private scope: `{agent_scope}`"
        tool_tips = f"""## Generic MCP Client Tips

- The MCP endpoint is `{base_url}/mcp`. Your client should send requests as JSON with `{{"tool": "...", "params": {{...}}}}`.
- Authenticate using `Authorization: Bearer ${ENV_PREFIX}API_KEY` header or your client's equivalent auth mechanism.
- Available tools include: `memory_search`, `memory_get`, `memory_write`, `memory_retract`, `credential_get`, `credential_list`, `activity_update`, `activity_list`, `get_briefing`, `briefing_list`, `connectors_list`, `connectors_summary`, `connectors_bindings_list`, `connectors_bindings_test`, `connectors_actions_list`, and `connectors_run`.
"""

    return f"""# {APP_NAME} Setup Instructions

You are connected to {APP_NAME}.

**User:** {user_display}
**Workspace:** {workspace_name}
**Agent:** {agent_display}
**Base URL:** {base_url}

## Scopes

{scope_guide}

## Getting Started

1. Call `activity_pickup` at startup or when idle to check for work a human has assigned to you in this workspace. If it returns an activity, that is your current task — read it, start working, and send heartbeats. If it returns null, there is no assigned work and you can proceed with whatever the user is asking.
2. Search memory in `{default_scope}` for relevant context before starting work. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
3. Search memory in `{user_scope}` for relevant user preferences and owner-context details when you have user-scope read access.
4. Create or update an activity record when starting a meaningful task. Use `task_note` for in-flight progress updates and `task_result` when closing the task.
5. Store durable decisions and handoff notes in `{default_scope}`.
6. Use `credential_list` and `credential_get` to retrieve credential references — never ask for raw secrets.
7. Use connector tools to discover and run available server-side connector bindings before asking the user to wire an external service manually.

## Memory Write Rules

- Choose `decision` for durable choices and rationale.
- Choose `fact` for objective workspace state or implementation details.
- Choose `preference` for stable user or team preferences.
- Choose `scratchpad` only for temporary notes.
- Use `{workspace_scope_label}` for workspace memory when a workspace is selected, `{user_scope}` for stable user preferences, and `{agent_scope}` for private scratch context.
- Domain and topic are optional exact-match search filters. Add them only when they will help future retrieval.
- Confidence is caller-assigned and can be filtered by search; importance affects result ranking.

## Credentials And Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.

## API Key

Set `{ENV_PREFIX}API_KEY` in your local environment using the key shown when this agent was created or rotated.
Do not commit API keys to workspace files.

{tool_tips}
## Tool Configuration

- MCP endpoint: {base_url}/mcp
- REST base: {base_url}
- Auth: Bearer token with your agent API key
"""


def _build_user_instructions(
    target,
    base_url,
    user_scope,
    workspace_scope,
    agent_scope,
    agent_display,
    user_display,
    workspace_name,
):
    workspace_line = (
        f"This setup is workspace-aware. Generated prompts use `{workspace_scope}` for workspace facts, decisions, and shared collaboration."
        if workspace_scope
        else f"No workspace is selected. Generated prompts use `{user_scope}` as read-only owner context and keep shared facts/decisions in the default shared scope."
    )
    return f"""# {APP_NAME} Instructions

Use these steps to connect an AI tool to {APP_NAME} for {user_display}.

## What To Generate

1. Generate `MCP Config` when you are ready to connect the tool to {APP_NAME}. This is the normal connection step for MCP-capable tools.
2. Generate `Environment Variables` only when your MCP config or launcher reads values from environment variables.
3. Generate `CLAUDE.md`, `AGENTS.md`, or `Assistants` guidance for reusable repository-level or agent-level instructions.
4. Generate `Session Prompt` when you want one-time agent-specific instructions pasted into a chat/session.
5. Generate `Verification Prompt` after setup and paste it into the connected agent to run the end-to-end verification flow.

Prompts can steer behavior and verify connectivity. They cannot install MCP servers, plugins, or skills by themselves.

## Connection Key

Click the one-time key button on `MCP Config` when you are ready to connect the tool.
Use the one-time key button on `Environment Variables` only when you need shell or launcher environment variables.
That rotates this agent's API key and inserts the new key into the generated output.
The key is shown once. Generating again invalidates the previous key for this connection.
The API key is the authoritative agent identity. {APP_NAME} identifies requests as `{agent_scope}` by looking up the bearer token; repo instruction files do not set identity.

## Where Things Go

- MCP Config belongs in the MCP configuration location for the tool you are connecting. For Claude Code, run `claude mcp add` (CLI) or create `.mcp.json` in the repo root; for Codex CLI, that is `~/.codex/config.toml`; for OpenCode, add the OpenCode block under `mcp` in `~/.config/opencode/opencode.json`.
- Environment variables belong in your shell profile, launcher, service environment, or tool-specific environment settings. They do not connect {APP_NAME} by themselves.
- Session Prompt is pasted into the first message or custom instructions for a single session. Use it to bootstrap behavior when no plugin or hook layer exists.
- `CLAUDE.md` and `AGENTS.md` belong in the workspace/repository root when you want persistent per-repository behavior. `Assistants` guidance is for assistant-style agents that manage their own config. These files are workspace-centric and can be shared by Codex, OpenCode, Claude Code, and other agents using their own MCP keys.

## Selected Context

- {APP_NAME} URL: `{base_url}`
- Connection agent for generated MCP/env output: `{agent_scope}`
- User: `{user_scope}`
- Workspace: `{workspace_scope or "optional / not selected"}`

Use the full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs as memory scopes.

{workspace_line}

If the client supports hooks or plugins, they can automate some of these behaviors. If not, this session prompt should be used to seed the same expectations manually.
"""


def _build_session_prompt(
    target,
    base_url,
    user_scope,
    workspace_scope,
    agent_scope,
    agent_display,
    user_display,
    workspace_name,
):
    default_scope = workspace_scope or user_scope
    tool_line = {
        "claude_code": "You are Claude Code.",
        "codex": "You are Codex.",
        "cursor": "You are Cursor's AI agent.",
        "windsurf": "You are Windsurf's AI agent.",
    }.get(target, "You are an MCP-capable AI agent.")
    return f"""{tool_line} You are working for {user_display} on {workspace_name}.

Use {APP_NAME} MCP for durable workspace memory, handoffs, and workspace context. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
Default memory scope for this setup is `{default_scope}`.
Core MCP tools include `memory_search`, `memory_get`, `memory_write`, `memory_retract`, `credential_get`, `credential_list`, `activity_update`, `activity_list`, `get_briefing`, `briefing_list`, `connectors_list`, `connectors_summary`, `connectors_bindings_list`, `connectors_bindings_test`, `connectors_actions_list`, and `connectors_run`.
Use your private scope `{agent_scope}` only for tool-specific scratch context.
Use full prefixed scope names exactly as shown; do not use plain workspace IDs or agent IDs as memory scopes.
Read `{user_scope}` for stable {user_display} preferences and other owner-context details when you have user-scope read access.
Use credential references through {APP_NAME} MCP; never request or print raw secrets.
Activity records are operational task tracking, not durable memory. At the start of every non-trivial user task, immediately call `activity_update` with a concise `task_description`, `memory_scope` set to `{default_scope}`, and `status` set to `active`. If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. While actively working, use `task_note` for short progress updates and call `activity_update` again every 1-2 minutes as a heartbeat. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete, or `status: blocked` if you cannot proceed and need user input.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client has hooks or plugins, use them to automate memory/activity capture; if it does not, treat this prompt as the source of truth for those expectations.
When an external service is needed, use `credential_list`, `credential_get`, `connectors_summary`, `connectors_list`, `connectors_bindings_list`, `connectors_actions_list`, `connectors_bindings_test`, and `connectors_run` instead of asking the user to hand-wire secrets or run manual service calls.

## Memory Workflow

At the start of a meaningful task:

1. Start or refresh the activity record using `activity_update`.
2. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
3. Search `{user_scope}` only when you have user-scope read access and user preferences or personal workflow may matter.
4. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class (there is no fetch-by-id).

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in `{default_scope}` for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope only if your key has user-scope write; otherwise treat the user scope as read-only owner context and write the preference to `{default_scope}` instead.
- `scratchpad` in `{agent_scope}` for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes. Use concise content, add domain/topic when useful for exact filtering, set confidence to match certainty, and set importance higher only for information likely to matter later.
Use this prompt to bootstrap behavior in clients without lifecycle hooks; it is not a substitute for a configured MCP server or plugin.

Start by confirming you can reach {APP_NAME} at {base_url}/mcp, then search `{default_scope}` for relevant context before making changes.
"""


def _build_claude_md(
    base_url, user_scope, workspace_scope, agent_scope, agent_display, workspace_name
):
    default_scope = (
        workspace_scope
        or f"the authenticated/default user scope from your {APP_NAME} connection"
    )
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    private_scope_guidance = f"Use your authenticated {APP_NAME} private scope, usually `agent:<your-agent-id>`, only for tool-specific scratch context."
    return f"""# {APP_NAME} Workspace Context

You are working on the {workspace_name} workspace.

Use {APP_NAME} for durable workspace memory, activity tracking, handoffs, and credential references. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
Core MCP tools include `memory_search`, `memory_get`, `memory_write`, `memory_retract`, `credential_get`, `credential_list`, `activity_update`, `activity_list`, `get_briefing`, `briefing_list`, `connectors_list`, `connectors_summary`, `connectors_bindings_list`, `connectors_bindings_test`, `connectors_actions_list`, and `connectors_run`.

## Connection

- **{APP_NAME} URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active {APP_NAME} user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not add API keys to this file.

## Memory Scopes

Use `{default_scope}` for default memory in this setup.
Read the authenticated/default user scope from your {APP_NAME} connection for stable personal preferences and owner-context details when you have user-scope read access.
{private_scope_guidance}
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs like `{agent_display}` as memory scopes.

## Memory Workflow

At the start of a meaningful task:

1. Start or refresh the activity record using the Activity Tracking workflow below.
2. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
3. Search the authenticated/default user scope only when you have user-scope read access and user preferences or personal workflow may matter.
4. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class (there is no fetch-by-id).

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in `{default_scope}` for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope only if your key has user-scope write; otherwise treat the user scope as read-only owner context and write the preference to `{default_scope}` instead.
- `scratchpad` in the authenticated private agent scope for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.

Keep memory content concise. Add domain/topic when useful for exact filtering. Set confidence to match certainty. Set importance higher only for information likely to matter later.

## Credentials

Use `credential_get` to retrieve `{CREDENTIAL_PREFIX}*` references. The Credential Broker resolves them at execution time.
Never ask users for raw credential values.

## Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.

## Activity Tracking

Activity records are operational task tracking, not durable memory.

At the start of every non-trivial user task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{default_scope}`
- `status`: `active`

When the task is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes. Use workspace memory, not agent-private scratch notes, as the durable source of truth for prior work.
Use `activity_list` and `briefing_list` when you need to inspect that trail from MCP instead of the dashboard.

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Use `task_note` for interim progress updates and update `task_description` if the task changes materially.

If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete. Use `task_note` for in-flight updates. Use `status: blocked` if you cannot proceed and need user input. Do not create activity records for trivial one-shot answers that do not inspect or modify project state.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client supports hooks or plugins, use them to automate these calls. If it does not, keep using this file as the manual operating contract.


## Claude Code Notes

- Claude Code automatically reads this `CLAUDE.md` file when present in the workspace root.
- Claude Code uses the configured MCP connection or your shell environment's `{ENV_PREFIX}API_KEY`. That key determines which {APP_NAME} user and agent are active.
- Do not add your API key to this file.
- **Tool availability:** Claude Code defers MCP tool schemas at startup to save context. Before calling any {APP_NAME} tool in a new session, load the schemas with `ToolSearch("select:mcp__{APP_SLUG}__memory_search,mcp__{APP_SLUG}__activity_update,mcp__{APP_SLUG}__memory_write")`. Skipping this causes `InputValidationError`. Add other tool names to the select list as needed.
- If Claude Code can't reach {APP_NAME}, run the Verification Prompt output to verify the full end-to-end setup.
"""


def _build_agents_md(
    base_url, user_scope, workspace_scope, agent_scope, workspace_name
):
    default_scope = (
        workspace_scope
        or f"the authenticated/default user scope from your {APP_NAME} connection"
    )
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    return f"""# {APP_NAME} Workspace Context

You are working on the {workspace_name} workspace.

## {APP_NAME}

Use {APP_NAME} MCP for memory, credential references, and activity tracking. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.

- **Base URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active {APP_NAME} user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not commit credentials to this file.
If your host defers tool availability, run its tool discovery/load step first so the {APP_NAME} MCP tools are available before you try to call them.

## Memory Scope Guidance

Default memory scope for this setup is `{default_scope}`.
Read the authenticated/default user scope from your {APP_NAME} connection for stable personal preferences and owner-context details when you have user-scope read access.
Use your authenticated {APP_NAME} private scope, usually `agent:<your-agent-id>`, for private scratch notes only.
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs or agent IDs as memory scopes.

## Activity Workflow

Activity records are operational task tracking, not durable memory.

At the start of every non-trivial user task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{default_scope}`
- `status`: `active`

When the task is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes. Use workspace memory, not agent-private scratch notes, as the durable source of truth for prior work.
Use `activity_list` and `briefing_list` when you need to inspect that trail from MCP instead of the dashboard.

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Use `task_note` for interim progress updates and update `task_description` if the task changes materially.

If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete. Use `task_note` for in-flight updates. Use `status: blocked` if you cannot proceed and need user input. Do not create activity records for trivial one-shot answers that do not inspect or modify project state.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client supports hooks or plugins, use them to automate these calls. If it does not, keep using this file as the manual operating contract.

## Memory Workflow

At the start of a meaningful task:

1. Confirm {APP_NAME} is reachable at {base_url}/mcp if this is a new setup or connectivity is uncertain.
2. Start or refresh the activity record using the Activity Workflow above.
3. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
4. If this is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes.
5. Search the authenticated/default user scope only when you have user-scope read access and user preferences or personal workflow may matter.
6. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class (there is no fetch-by-id).

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in `{default_scope}` for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope only if your key has user-scope write; otherwise treat the user scope as read-only owner context and write the preference to `{default_scope}` instead.
- `scratchpad` in the authenticated private agent scope for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.

Keep memory content concise. Add domain/topic when useful for exact filtering. Set confidence to match certainty. Set importance higher only for information likely to matter later.

## Credentials And Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.
This file is the manual fallback when the client has no lifecycle hook or plugin layer.

## Codex Notes

- Codex reads `AGENTS.md` at the start of each session.
- This file is workspace-centric and can be shared by multiple agents in the same repository. The MCP/API key determines whether the active agent is Codex, OpenCode, Claude Code, or another configured agent.
- For multi-agent collaboration, select a workspace and ensure each agent has read/write access to that workspace scope.
- Use the MCP tools (`memory_search`, `memory_write`, `activity_update`, `credential_list`, `credential_get`, `connectors_*`) rather than raw API calls for better scope enforcement.
- If Codex loses connectivity, run the verification prompt to verify the full end-to-end setup.
"""


def _build_assistants_md(base_url, user_scope, workspace_scope, agent_scope, api_key=None, default_recall_scopes_json=None):
    # Operational writes (activity tracking, searches) go to the writable scope an
    # agent actually has: the selected workspace when one is chosen, otherwise its
    # own agent scope. Durable, shareable KNOWLEDGE is different: it belongs in a
    # workspace, never in the private agent scope. Agents are not granted user-scope
    # or workspace write by default, so the user scope is read-only unless granted.
    durable_scope = workspace_scope or agent_scope
    # Where the prompt tells the agent to put durable facts/decisions. With a
    # workspace, that is the workspace. Without one, we must NOT present the private
    # agent scope as a durable store (that mistake silos owner knowledge); we point
    # the agent at requesting a workspace instead.
    durable_label = (
        f"`{workspace_scope}`"
        if workspace_scope
        else "your assigned workspace (ask the owner to create or grant one — not your private agent scope)"
    )
    durable_guidance = (
        f"Write durable, shareable memory to `{workspace_scope}` (the selected workspace)."
        if workspace_scope
        else (
            f"No workspace is selected, so you have no shared durable store yet. Ask the"
            f" owner to create or select a workspace and grant you write before storing"
            f" shared or owner-facing facts; keep only your own agent-local notes in"
            f" `{agent_scope}` until then."
        )
    )
    recall_list = None
    if default_recall_scopes_json:
        try:
            parsed_recall = json.loads(default_recall_scopes_json)
            if isinstance(parsed_recall, list) and parsed_recall:
                recall_list = parsed_recall
        except (TypeError, ValueError):
            recall_list = None
    if recall_list:
        recall_intro = (
            "Default vs on-demand recall: your configured default recall scopes are "
            + ", ".join(f"`{s}`" for s in recall_list)
            + " — an unscoped `memory_search`/`memory_get` recalls only these."
        )
    else:
        recall_intro = (
            f"Default vs on-demand recall: your everyday recall scopes are `{durable_scope}` "
            f"(your own working store) and `{user_scope}` (owner facts)."
        )
    connection_key = _connection_key_value(api_key)
    workspace_context_line = (
        f"- Workspace scope: `{workspace_scope}` (use this for shared collaboration in the selected workspace)."
        if workspace_scope
        else ""
    )
    return f"""# {APP_NAME} Assistant Onboarding

Use {APP_NAME} as the durable backend for assistant-style agents that manage their own MCP configuration.

## {APP_NAME}

- **Base URL:** {base_url}
{workspace_context_line}

## Connection Values

- **MCP URL:** {base_url}/mcp
- **Bearer token:** {connection_key}

Use the one-time key button when you need a fresh bearer token. The generated output should use the value above directly.

The active {APP_NAME} user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not add API keys to this file.
If your host defers tool availability, run its tool discovery/load step first so the {APP_NAME} MCP tools are available before you try to call them.

## Memory Scope Guidance

The guiding rule: anything that is shared across agents or tools by default, or that belongs to the owner or a shared domain rather than to you, belongs in a `workspace:<id>`. Your `{agent_scope}` scope is for your own scratch, operational state, and self-knowledge; it is private by default (another agent can read it only if granted that scope), so it is not the home for the owner's personal facts or for knowledge several agents should share.

{durable_guidance}
Read `{user_scope}` for stable owner preferences and owner-context details. Treat the user scope as read-only unless your key was explicitly granted user-scope write; it is for facts about the owner, not a general shared store.
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs or agent IDs as memory scopes.

{recall_intro} Your key may ALSO be granted read access to other workspaces — other projects, or other agents' work — but those are NOT in your default recall. When the owner asks you something general, answer only from your default scopes. Reach into another scope ONLY when the request is explicitly about that project or topic, by naming it: `memory_search(scope="workspace:<id>")` or `memory_get(scope="workspace:<id>")`. Treat other-project access as on-demand, never the default — an unscoped search deliberately will not return them.

## Setup

1. Add {APP_NAME} as an MCP server using the connection values provided for this session.
2. Update your own MCP configuration in the location your agent normally uses.
3. Reload or restart the agent as supported.
4. Verify that the {APP_NAME} server is visible before doing any {APP_NAME} work.

## Operating Rules

Activity records are operational task tracking, not durable memory.

At the start of every meaningful task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{durable_scope}`
- `status`: `active`

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Use `task_note` for interim progress updates and update `task_description` if the task changes materially.

If the session reloads, a handoff begins, or no active activity exists yet, open a fresh activity first with `status: active` before attempting to close it. Before your final response, call `activity_update` with `status: completed` and a short `task_result` summary when the task is complete. Use `task_note` for in-flight updates. Use `status: blocked` if you cannot proceed and need user input.

At the start of a meaningful task:

1. Search `{durable_scope}` with focused queries for relevant context, prior decisions, and current state, and search `{user_scope}` for owner preferences and context. If a search returns little, retry with exact topic values or exact keywords from prior records.
2. Stay in your default scopes by default. Do not fan recall across other workspaces your key can read. Only when the request is explicitly about another project, search that project's `workspace:<id>` by naming it directly; otherwise an unscoped search will mix unrelated projects into your answer.
3. If this is a handoff, resume, or review of prior work, inspect `activity_list` and `briefing_list` before making changes.
4. Use `memory_get` with a scope to list or read records; use `memory_search` to find records by query, topic, or class. There is no fetch-by-id.

Write memory only when it will help a future session:

- `decision` in {durable_label} for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in {durable_label} for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in `{user_scope}` only if your key has user-scope write; otherwise write it to {durable_label}. Preferences support `slot_key` to keep one active value per slot (`slot_key` is valid for the `preference` class only).
- `scratchpad` in your agent scope `{agent_scope}` for temporary private notes.
- To revise a `fact` or `decision`, write the new record with `supersedes_id` set to the prior record's id; reserve `memory_retract` for records that are simply wrong.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check {APP_NAME} before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `{CREDENTIAL_PREFIX}*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_summary` for a compact capability overview, or `connectors_list` and `connectors_bindings_list` when you need raw connector and binding lists.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when {APP_NAME} should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside {APP_NAME}.
"""


def _connection_key_value(api_key=None):
    return api_key or "{{" + ENV_PREFIX + "API_KEY}}"


def _build_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    _shell_api_key = "${" + ENV_PREFIX + "API_KEY}"
    codex_auth = f'http_headers = {{ Authorization = "Bearer {key}" }}'
    generic_json = json.dumps(
        {
            "mcpServers": {
                APP_SLUG: {
                    "type": "http",
                    "url": f"{base_url}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {key}",
                    },
                },
            },
        },
        indent=2,
    )
    opencode_json = json.dumps(
        {
            "mcp": {
                APP_SLUG: {
                    "type": "remote",
                    "url": f"{base_url}/mcp",
                    "enabled": True,
                    "headers": {
                        "Authorization": f"Bearer {key}",
                    },
                },
            },
        },
        indent=2,
    )
    antigravity_json = json.dumps(
        {
            "mcpServers": {
                APP_SLUG: {
                    "type": "http",
                    "serverUrl": f"{base_url}/mcp",
                    "headers": {
                        "Authorization": f"Bearer {key}",
                    },
                },
            },
        },
        indent=2,
    )
    return f"""# Claude Code
# Option 1 — CLI (recommended, adds to user-level config so it works in every project):
claude mcp add --transport http --scope user {APP_SLUG} {base_url}/mcp \\
  --header "Authorization: Bearer {key}"

# Option 2 — create .mcp.json in your repo root (workspace-level, committable):
# If committing, replace the key with an env var: "Bearer {_shell_api_key}"
{generic_json}

# Codex CLI: add this to ~/.codex/config.toml
[mcp_servers.{APP_SLUG}]
url = "{base_url}/mcp"
{codex_auth}

# OpenCode: add this under ~/.config/opencode/opencode.json
{opencode_json}

# Antigravity/Other MCP clients:
# Use this JSON block. File location varies by tool — see docs/integrations.md.
{antigravity_json}
"""


def _build_cursor_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    _api_key_var = ENV_PREFIX + "API_KEY"
    _url_var = ENV_PREFIX + "URL"
    return f"""{{
  "mcpServers": {{
    "{APP_SLUG}": {{
      "url": "{base_url}/mcp",
      "headers": {{
        "Authorization": "Bearer {key}"
      }},
      "env": {{
        "{_api_key_var}": "{key}",
        "{_url_var}": "{base_url}"
      }}
    }}
  }}
}}"""


def _build_windsurf_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    _api_key_var = ENV_PREFIX + "API_KEY"
    _url_var = ENV_PREFIX + "URL"
    return f"""{{
  "mcpServers": {{
    "{APP_SLUG}": {{
      "url": "{base_url}/mcp",
      "headers": {{
        "Authorization": "Bearer {key}"
      }},
      "env": {{
        "{_api_key_var}": "{key}",
        "{_url_var}": "{base_url}"
      }}
    }}
  }}
}}"""


def _build_env_vars(base_url, agent_id, user_scope, workspace_scope, api_key=None):
    key = _connection_key_value(api_key)
    workspace_line = (
        f'export {ENV_PREFIX}WORKSPACE_SCOPE="{workspace_scope}"'
        if workspace_scope
        else f'# export {ENV_PREFIX}WORKSPACE_SCOPE="workspace:your-workspace-id"  # Optional'
    )
    return f"""# {APP_NAME} Environment Variables
# Use these only when your MCP config, launcher, or script reads {APP_NAME} values from the environment.
# MCP config is still the normal connection setup for MCP-capable tools.
# Do not commit these to workspace files.
#
# {ENV_PREFIX}API_KEY is the real authenticated agent identity.
# {ENV_PREFIX}AGENT_ID is helper metadata only; the server does not trust it for identity.

export {ENV_PREFIX}URL="{base_url}"
export {ENV_PREFIX}API_KEY="{key}"
export {ENV_PREFIX}AGENT_ID="{agent_id}"
export {ENV_PREFIX}USER_SCOPE="{user_scope}"
{workspace_line}
"""


def _get_destination_guidance(target, output_type):
    if output_type == "claude_md":
        return f"Save this as <code>CLAUDE.md</code> in the workspace repository root. It is workspace-centric; the configured MCP/API key determines the active {APP_NAME} agent."
    elif output_type == "agents_md":
        return f"Save this as <code>AGENTS.md</code> in the workspace repository root. It is workspace-centric and can be shared by multiple agents; the configured MCP/API key determines the active {APP_NAME} agent."
    elif output_type == "assistants_md":
        return "Paste this into the assistant's own onboarding or instruction field. It is meant for assistant-style agents that manage their own MCP configuration. Use the one-time key button here when you want the prompt to include a fresh bearer token."
    elif output_type == "mcp_json":
        return f"Use the section that matches your tool. For Claude Code, run the <code>claude mcp add</code> command or save the JSON as <code>.mcp.json</code> in your repo root. Antigravity uses the same MCP shape but expects <code>serverUrl</code> instead of <code>url</code>. The bearer key determines the active {APP_NAME} agent."
    elif output_type == "env":
        return "Optional. Paste these into your shell profile (<code>~/.bashrc</code>, <code>~/.zshrc</code>) or tool environment only if you want the tool to read values from environment variables. This does not configure MCP by itself."
    elif output_type == "instructions":
        return "Read this yourself. It explains which generated output to use, where to put it, and when to generate a one-time key."
    elif output_type == "session":
        return "Paste this into the first message or workspace instructions field for a single agent session."
    elif output_type == "verification":
        return "Paste this into your connected agent after setup. It runs the end-to-end verification flow and reports memory, credential, and connector access."
    return ""


def _build_verification_prompt(user_scope, workspace_scope):
    return f"""Run the {APP_NAME} verification flow end to end:

1. Call `activity_update` with `task_description` set to "{APP_NAME} verification", `memory_scope` set to `{workspace_scope}`, and `status` set to `active`.
2. Write a memory record to `{workspace_scope}` that says this agent is connected, includes the current verification context, and is safe to use for this workspace. Capture the returned record id.
3. Call `memory_get` for that record id and confirm the record is readable from the workspace scope.
4. Call `memory_search` in `{workspace_scope}` for an exact token from the record you just wrote and report the result. If the first search returns zero results, retry once with the exact token plus the `domain` and `topic` from the record.
5. Call `credential_list` and report whether credential references are visible. Do not reveal or print raw secrets.
6. Call `connectors_summary` to list visible connector capability and binding health. Then call `connectors_bindings_list` with no scope to see everything visible to this agent. If you can read `{user_scope}`, call `connectors_bindings_list` again with `scope` set to `{user_scope}`. If you can read `{workspace_scope}`, call `connectors_bindings_list` again with `scope` set to `{workspace_scope}`. Report user-scoped and workspace-scoped bindings separately if both exist.
7. Call `connectors_actions_list` with a real connector type id from the `connectors_list` result and pass it as `connector_type_id` exactly. Report whether connector actions are visible.
8. If at least one enabled binding is visible in any scope, call `connectors_bindings_test` on a non-destructive binding and report the result. If none are visible, say that clearly.
9. Use `task_note` for intermediate verification updates and call `activity_update` with `status` set to `completed` and include a short `task_result` summary of the verification outcome. If the session reloads or no active activity exists yet, first open the fresh verification activity with `status: active` before closing it.
10. Report which scope you wrote to and summarize the memory, credential, and connector checks.

Use the full prefixed scope name exactly as shown. Do not use a plain workspace ID as a memory scope.
"""


# ─── WEBHOOKS ─────────────────────────────────────────────────────────────────


@router.get("/webhooks")
async def webhooks_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import webhook_service as wh_svc

    if session.get("role") != "admin":
        return render_page(
            "Admin Required",
            """
<div class="page-header"><h1>Admin Access Required</h1></div>
<div class="card"><p>Webhook management is restricted to administrators.</p>
<a href="/" class="btn">Back to Overview</a></div>""",
            nav_active="/webhooks",
            session=session,
            status_code=403,
        )

    from app.services import inbound_webhook_service as inbound_svc

    webhooks = wh_svc.list_webhooks()
    event_types = wh_svc.WEBHOOK_EVENT_TYPES

    base_url = str(request.base_url).rstrip("/")
    inbound_url = f"{base_url}/api/webhooks/inbound"
    inbound_key_row = inbound_svc.get_active_key_row()
    inbound_has_key = inbound_key_row is not None
    inbound_key_created = inbound_key_row["created_at"][:19] if inbound_key_row else ""
    inbound_key_rotated = inbound_key_row["rotated_at"][:19] if (inbound_key_row and inbound_key_row.get("rotated_at")) else ""

    event_type_options = "".join(
        f'<label class="checkbox-label"><input type="checkbox" name="event_types" value="{e}"> {e}</label>'
        for e in event_types
    )

    rows = ""
    for wh in webhooks:
        enabled_badge = (
            "<span class='badge badge-active'>enabled</span>"
            if wh["enabled"]
            else "<span class='badge badge-stale'>disabled</span>"
        )
        events_str = ", ".join(f"<code>{e}</code>" for e in wh["event_types"]) or "—"
        wh_id = wh["id"]
        wh_name_js = escape_html(wh["name"]).replace("'", "\\'")
        rows += (
            "<tr>"
            f"<td><strong>{escape_html(wh['name'])}</strong></td>"
            f"<td><code class='url-cell'>{escape_html(wh['url'])}</code></td>"
            f"<td>{events_str}</td>"
            f"<td>{enabled_badge}</td>"
            "<td>"
            f"<div class='actions-cell'>"
            f"<button class='btn btn-sm' onclick=\"openEditWebhook('{wh_id}')\">Edit</button> "
            f"<button class='btn btn-sm btn-secondary' onclick=\"openTestWebhook('{wh_id}')\">Test</button> "
            f"<button class='btn btn-sm btn-secondary' onclick=\"viewDeliveries('{wh_id}', '{wh_name_js}')\">Deliveries</button> "
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick=\"deleteWebhook('{wh_id}', '{wh_name_js}')\" title='Delete webhook' aria-label='Delete webhook'>{get_icon('delete')}</button>"
            f"</div>"
            "</td>"
            "</tr>"
        )

    if not rows:
        rows = "<tr><td colspan='5' style='text-align:center;color:var(--text-muted)'>No webhooks registered yet.</td></tr>"

    inbound_key_status_html = (
        f"<span class='badge badge-active'>Active</span> &nbsp;Generated {inbound_key_created}"
        + (f" &nbsp;· Last rotated {inbound_key_rotated}" if inbound_key_rotated else "")
        if inbound_has_key else "<span class='badge badge-stale'>No key</span>"
    )
    inbound_key_btn = (
        "<button class='btn btn-sm btn-secondary' onclick='rotateInboundKey()'>Rotate Key</button>"
        if inbound_has_key else
        "<button class='btn btn-sm' onclick='generateInboundKey()'>Generate Key</button>"
    )

    body = f"""
<div class="page-header">
  <h1>Webhooks</h1>
  <button class="btn" onclick="document.getElementById('create-webhook-modal').style.display='flex'">+ New Webhook</button>
</div>

<!-- Inbound section -->
<div class="card" style="margin-bottom:1.5rem">
  <h3 style="margin-top:0">Inbound Receiver</h3>
  <p>External systems (n8n, Zapier, custom scripts) can push work commands into {APP_NAME} using the inbound webhook endpoint. Authenticate requests with the <code>X-Agent-Core-Inbound-Key</code> header.</p>
  <div style="margin-bottom:1rem">
    <label style="display:block;margin-bottom:0.35rem;font-weight:600">Inbound URL</label>
    <div style="display:flex;gap:0.5rem;align-items:center">
      <code id="inbound-url" style="flex:1;padding:0.5rem;background:var(--bg-secondary);border-radius:4px;word-break:break-all">{escape_html(inbound_url)}</code>
      <button class="btn btn-sm btn-secondary" onclick="copyInboundUrl()">Copy URL</button>
    </div>
  </div>
  <div style="display:flex;align-items:center;gap:1rem;flex-wrap:wrap">
    <span><strong>Key status:</strong> {inbound_key_status_html}</span>
    {inbound_key_btn}
  </div>
  <div id="inbound-key-reveal" style="display:none;margin-top:1rem;padding:0.75rem;background:var(--bg-secondary);border-radius:4px;border-left:3px solid var(--warning-color)">
    <strong>New key (shown once):</strong>
    <code id="inbound-key-value" style="display:block;word-break:break-all;margin:0.35rem 0"></code>
    <button class="btn btn-sm btn-secondary" onclick="copyInboundKey()">Copy Key</button>
    <p style="margin:0.5rem 0 0;color:var(--text-muted);font-size:0.85em">Store this key now. It will not be shown again.</p>
  </div>
  <p style="margin-top:1rem;margin-bottom:0.5rem;color:var(--text-muted);font-size:0.85em">
    Supported commands: <code>activity.create</code>, <code>activity.assign</code>, <code>activity.update</code>, <code>activity.cancel</code>, <code>activity.note</code>
  </p>
  <details style="margin-top:0.75rem">
    <summary style="cursor:pointer;font-size:0.9em;color:var(--text-muted)">How to send a command</summary>
    <div style="margin-top:0.75rem;font-size:0.85em">
      <p style="margin:0 0 0.5rem">POST to the inbound URL with your key in the header and a JSON body:</p>
      <pre style="background:var(--bg-secondary);padding:0.75rem;border-radius:4px;overflow-x:auto;margin:0 0 0.75rem"><code>POST {escape_html(inbound_url)}
X-Agent-Core-Inbound-Key: &lt;your-key&gt;
Content-Type: application/json

{{
  "event_type": "activity.create",
  "assigned_agent_id": "my-agent",
  "task_description": "Review the latest support tickets",
  "memory_scope": "workspace:my-project"
}}</code></pre>
      <p style="margin:0;color:var(--text-muted)">The assigned agent picks up the task on its next <code>activity_pickup</code> call. Other commands (<code>activity.cancel</code>, <code>activity.note</code>, etc.) require an <code>activity_id</code> from the create response.</p>
    </div>
  </details>
</div>

<!-- Outbound section -->
<h2 style="margin-bottom:0.75rem">Outbound Notifications</h2>
<div class="card" style="margin-bottom:1.5rem">
  <p>Outbound webhook notifications let external systems react to {APP_NAME} events. Each registered endpoint receives a signed HTTP POST when a subscribed event occurs. Webhooks are admin-only and fire-and-log — no retries, no orchestration.</p>
  <p><strong>Signing:</strong> Every delivery includes <code>X-Agent-Core-Signature: sha256=&lt;hex&gt;</code> so receivers can verify authenticity using HMAC-SHA256 with the stored secret.</p>
</div>
<div class="card">
  <table class="data-table">
    <thead><tr>
      <th>Name</th><th>URL</th><th>Events</th><th>Status</th><th class="actions-cell">Actions</th>
    </tr></thead>
    <tbody id="webhooks-table-body">{rows}</tbody>
  </table>
</div>

<!-- Create webhook modal -->
<div class="modal-overlay" id="create-webhook-modal" style="display:none">
  <div class="modal">
    <h3>New Webhook</h3>
    <div id="create-webhook-error" class="error-box" style="display:none"></div>
    <label>Name
      <input type="text" id="wh-name" placeholder="e.g. n8n Activity Alerts">
    </label>
    <label>URL
      <input type="text" id="wh-url" placeholder="https://your-endpoint.example.com/hook">
    </label>
    <label>Secret <span style="color:var(--text-muted);font-size:0.85em">(used for HMAC-SHA256 signature)</span>
      <input type="password" id="wh-secret" placeholder="Enter a strong secret">
    </label>
    <label>Subscribe to events</label>
    <div class="checkbox-group" id="wh-event-types">
      {event_type_options}
    </div>
    <div class="modal-actions">
      <button class="btn" onclick="createWebhook()">Create</button>
      <button class="btn btn-secondary" onclick="document.getElementById('create-webhook-modal').style.display='none'">Cancel</button>
    </div>
  </div>
</div>

<!-- Edit webhook modal -->
<div class="modal-overlay" id="edit-webhook-modal" style="display:none">
  <div class="modal">
    <h3>Edit Webhook</h3>
    <div id="edit-webhook-error" class="error-box" style="display:none"></div>
    <input type="hidden" id="edit-webhook-id">
    <label>Name
      <input type="text" id="edit-wh-name">
    </label>
    <label>URL
      <input type="text" id="edit-wh-url">
    </label>
    <label>New Secret <span style="color:var(--text-muted);font-size:0.85em">(leave blank to keep existing)</span>
      <input type="password" id="edit-wh-secret" placeholder="Leave blank to keep current secret">
    </label>
    <label>Subscribe to events</label>
    <div class="checkbox-group" id="edit-wh-event-types">
      {event_type_options.replace('name="event_types"', 'name="edit_event_types"').replace('id="', 'id="edit-')}
    </div>
    <label class="checkbox-label" style="margin-top:0.75rem">
      <input type="checkbox" id="edit-wh-enabled"> Enabled
    </label>
    <div class="modal-actions">
      <button class="btn" onclick="submitEditWebhook()">Save</button>
      <button class="btn btn-secondary" onclick="document.getElementById('edit-webhook-modal').style.display='none'">Cancel</button>
    </div>
  </div>
</div>

<!-- Deliveries modal -->
<div class="modal-overlay" id="deliveries-modal" style="display:none">
  <div class="modal" style="max-width:700px">
    <h3 id="deliveries-modal-title">Recent Deliveries</h3>
    <div id="deliveries-content" style="max-height:420px;overflow-y:auto"></div>
    <div class="modal-actions">
      <button class="btn btn-secondary" onclick="document.getElementById('deliveries-modal').style.display='none'">Close</button>
    </div>
  </div>
</div>

<!-- Test webhook modal -->
<div class="modal-overlay" id="test-webhook-modal" style="display:none">
  <div class="modal" style="max-width:420px">
    <h3>Test Webhook Delivery</h3>
    <input type="hidden" id="test-webhook-id">
    <p style="color:var(--text-muted);font-size:0.9em;margin-bottom:12px">Send a sample payload to verify your endpoint handles each event type correctly.</p>
    <label>Event type
      <select id="test-webhook-event-type" style="width:100%"></select>
    </label>
    <div id="test-webhook-result" style="margin-top:12px"></div>
    <div class="modal-actions">
      <button class="btn" onclick="submitTestWebhook()">Send Test</button>
      <button class="btn btn-secondary" onclick="document.getElementById('test-webhook-modal').style.display='none'">Close</button>
    </div>
  </div>
</div>
"""

    js = """
<script>
function copyToClipboard(text, label) {
  if (navigator.clipboard && window.isSecureContext) {
    navigator.clipboard.writeText(text).then(() => alert(label + ' copied.')).catch(() => fallbackCopy(text, label));
  } else {
    fallbackCopy(text, label);
  }
}

function fallbackCopy(text, label) {
  const ta = document.createElement('textarea');
  ta.value = text;
  ta.style.cssText = 'position:fixed;opacity:0';
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  try { document.execCommand('copy'); alert(label + ' copied.'); }
  catch { prompt('Copy this ' + label + ':', text); }
  document.body.removeChild(ta);
}

function copyInboundUrl() {
  copyToClipboard(document.getElementById('inbound-url').textContent.trim(), 'Inbound URL');
}

function copyInboundKey() {
  copyToClipboard(document.getElementById('inbound-key-value').textContent.trim(), 'Key');
}

async function generateInboundKey() {
  if (!confirm('Generate an inbound key? The key will be shown once.')) return;
  const r = await fetch('/api/webhooks/inbound/key', {method: 'POST'});
  const data = await r.json();
  if (!data.ok) { alert('Failed: ' + (data.error?.message || 'unknown')); return; }
  document.getElementById('inbound-key-value').textContent = data.data.key;
  document.getElementById('inbound-key-reveal').style.display = 'block';
  location.reload = () => {};  // suppress auto-reload so user can copy
}

async function rotateInboundKey() {
  if (!confirm('Rotate the inbound key? The previous key will stop working immediately.')) return;
  const r = await fetch('/api/webhooks/inbound/key/rotate', {method: 'POST'});
  const data = await r.json();
  if (!data.ok) { alert('Failed: ' + (data.error?.message || 'unknown')); return; }
  document.getElementById('inbound-key-value').textContent = data.data.key;
  document.getElementById('inbound-key-reveal').style.display = 'block';
  location.reload = () => {};
}

async function createWebhook() {
  const name = document.getElementById('wh-name').value.trim();
  const url = document.getElementById('wh-url').value.trim();
  const secret = document.getElementById('wh-secret').value;
  const errorBox = document.getElementById('create-webhook-error');
  errorBox.style.display = 'none';
  const eventTypes = Array.from(
    document.querySelectorAll('#wh-event-types input[type=checkbox]:checked')
  ).map(cb => cb.value);
  if (!name || !url || !secret || eventTypes.length === 0) {
    errorBox.textContent = 'Name, URL, secret, and at least one event type are required.';
    errorBox.style.display = 'block';
    return;
  }
  const r = await fetch('/api/webhooks', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({name, url, secret, event_types: eventTypes}),
  });
  const data = await r.json();
  if (!data.ok) {
    errorBox.textContent = data.error?.message || 'Failed to create webhook';
    errorBox.style.display = 'block';
    return;
  }
  document.getElementById('create-webhook-modal').style.display = 'none';
  location.reload();
}

function openEditWebhook(id) {
  fetch('/api/webhooks/' + id)
    .then(r => r.json())
    .then(data => {
      if (!data.ok) return;
      const wh = data.data.webhook;
      document.getElementById('edit-webhook-id').value = wh.id;
      document.getElementById('edit-wh-name').value = wh.name;
      document.getElementById('edit-wh-url').value = wh.url;
      document.getElementById('edit-wh-secret').value = '';
      document.getElementById('edit-wh-enabled').checked = wh.enabled;
      document.querySelectorAll('#edit-wh-event-types input[type=checkbox]').forEach(cb => {
        cb.checked = wh.event_types.includes(cb.value);
      });
      document.getElementById('edit-webhook-error').style.display = 'none';
      document.getElementById('edit-webhook-modal').style.display = 'flex';
    });
}

async function submitEditWebhook() {
  const id = document.getElementById('edit-webhook-id').value;
  const body = {
    name: document.getElementById('edit-wh-name').value.trim(),
    url: document.getElementById('edit-wh-url').value.trim(),
    enabled: document.getElementById('edit-wh-enabled').checked,
    event_types: Array.from(
      document.querySelectorAll('#edit-wh-event-types input[type=checkbox]:checked')
    ).map(cb => cb.value),
  };
  const secret = document.getElementById('edit-wh-secret').value;
  if (secret) body.secret = secret;
  const errorBox = document.getElementById('edit-webhook-error');
  errorBox.style.display = 'none';
  const r = await fetch('/api/webhooks/' + id, {
    method: 'PUT',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body),
  });
  const data = await r.json();
  if (!data.ok) {
    errorBox.textContent = data.error?.message || 'Update failed';
    errorBox.style.display = 'block';
    return;
  }
  document.getElementById('edit-webhook-modal').style.display = 'none';
  location.reload();
}

async function deleteWebhook(id, name) {
  if (!confirm('Delete webhook "' + name + '"? This cannot be undone.')) return;
  const r = await fetch('/api/webhooks/' + id, {method: 'DELETE'});
  const data = await r.json();
  if (data.ok) location.reload();
  else alert('Delete failed: ' + (data.error?.message || 'unknown error'));
}

async function openTestWebhook(id) {
  const r = await fetch('/api/webhooks/' + id);
  const data = await r.json();
  if (!data.ok) return;
  const wh = data.data.webhook;
  document.getElementById('test-webhook-id').value = id;
  const sel = document.getElementById('test-webhook-event-type');
  sel.innerHTML = '';
  const types = wh.event_types.length ? wh.event_types : ['activity_created','activity_updated','activity_heartbeat','activity_cancelled','activity_recovered','connector_executed'];
  types.forEach(t => {
    const opt = document.createElement('option');
    opt.value = t;
    opt.textContent = t;
    sel.appendChild(opt);
  });
  document.getElementById('test-webhook-result').innerHTML = '';
  document.getElementById('test-webhook-modal').style.display = 'flex';
}

async function submitTestWebhook() {
  const id = document.getElementById('test-webhook-id').value;
  const event_type = document.getElementById('test-webhook-event-type').value;
  const resultBox = document.getElementById('test-webhook-result');
  resultBox.innerHTML = '<span style="color:var(--text-muted)">Sending...</span>';
  const r = await fetch('/api/webhooks/' + id + '/test', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({event_type}),
  });
  const data = await r.json();
  if (data.data?.ok) {
    resultBox.innerHTML = '<span style="color:var(--success-color)">Delivered — HTTP ' + (data.data.http_status || '—') + '</span>';
  } else {
    const msg = data.data?.error || data.error?.message || 'unknown error';
    resultBox.innerHTML = '<span style="color:var(--danger-color)">Failed: ' + msg + '</span>';
  }
}

function viewDeliveries(id, name) {
  document.getElementById('deliveries-modal-title').textContent = 'Recent Deliveries — ' + name;
  document.getElementById('deliveries-content').innerHTML = '<p style="color:var(--text-muted)">Loading...</p>';
  document.getElementById('deliveries-modal').style.display = 'flex';
  fetch('/api/webhooks/' + id + '/deliveries?limit=30')
    .then(r => r.json())
    .then(data => {
      if (!data.ok || !data.data.deliveries.length) {
        document.getElementById('deliveries-content').innerHTML = '<p style="color:var(--text-muted)">No deliveries recorded yet.</p>';
        return;
      }
      const rows = data.data.deliveries.map(d => {
        const badge = d.status === 'success'
          ? "<span class='badge badge-active'>success</span>"
          : "<span class='badge badge-stale'>failure</span>";
        const detail = d.error_message ? `<br><small style='color:var(--text-muted)'>${d.error_message}</small>` : '';
        return `<tr><td>${localDt(d.delivered_at)}</td><td><code>${d.event_type}</code></td><td>${badge} ${d.http_status ? 'HTTP ' + d.http_status : ''}${detail}</td></tr>`;
      }).join('');
      document.getElementById('deliveries-content').innerHTML =
        '<table class="data-table"><thead><tr><th>Time</th><th>Event</th><th>Result</th></tr></thead><tbody>' + rows + '</tbody></table>';
    });
}
</script>
"""

    return render_page(
        "Webhooks",
        body,
        nav_active="/webhooks",
        extra_js=js,
        session=session,
    )
