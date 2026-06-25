"""Overview dashboard page and global search. Split from dashboard.py — see
private/dashboard-split-plan.md."""

from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel

from app.branding import APP_NAME, JS_WINDOW_EVENT
from app.security.context import RequestContext
from app.security.dependencies import get_request_context
from app.security.response_helpers import success_response, error_response
from app.security.scope_enforcer import ScopeEnforcer
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    local_dt,
    _json_loads,
    _query_in_values,
    _search_result,
)

router = APIRouter()


class DashboardSearchRequest(BaseModel):
    query: str
    limit: int = 5


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


