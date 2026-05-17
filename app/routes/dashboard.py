from fastapi import APIRouter, Request, Depends, HTTPException
from fastapi.responses import HTMLResponse
import httpx
import json
from collections import Counter
from urllib.parse import urlparse
from pydantic import BaseModel

from app.services.auth_service import validate_session, get_user_by_id, count_users
from app.security.context import RequestContext
from app.security.dependencies import get_request_context
from app.security.dependencies import require_admin
from app.security.response_helpers import success_response, error_response
from app.security.scope_enforcer import ScopeEnforcer
from app.config import settings


router = APIRouter()


def _hf(s):
    return s.replace("escape_html", "escapeHtml")


def escape_html(s):
    if s is None:
        return ""
    return (
        str(s)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def get_session_token(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    token = request.cookies.get("session_token")
    if token:
        return token
    return ""


def get_icon(name: str, size: int = 16, color: str = "currentColor") -> str:
    icons = {
        "delete": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18m-2 0v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6m3 0V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2m-6 9l4 4m0-4l-4 4"/></svg>',
        "logout": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M9 21H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h4m7 14l5-5-5-5m5 5H9"/></svg>',
        "edit": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7m-5-9l5 5L9 20H4v-5L15 3z"/></svg>',
        "view": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/><circle cx="12" cy="12" r="3"/></svg>',
        "settings": f'<svg width="{size}" height="{size}" viewBox="0 0 24 24" fill="none" stroke="{color}" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    }
    return icons.get(name, "")


def require_auth(request: Request):
    token = get_session_token(request)
    if not token:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    session = validate_session(
        token, inactivity_minutes=settings.INACTIVITY_TIMEOUT_MINUTES
    )
    if not session:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return session


def render_page(
    title: str,
    body: str,
    nav_active: str = "",
    extra_js: str = "",
    session: dict | None = None,
    status_code: int = 200,
    show_sidebar: bool = True,
) -> HTMLResponse:
    is_admin = bool(session and session.get("role") == "admin")
    extra_js = extra_js.replace("{{", "{").replace("}}", "}")
    nav_items = [
        ("/", "Overview"),
        ("/users", "Users"),
        ("/agents", "Agents"),
        ("/workspaces", "Workspaces"),
        ("/memory", "Memory"),
        ("/connectors", "Connectors"),
        ("/integrations", "Integrations"),
        ("/activity", "Activity"),
        ("/audit", "Audit"),
        ("/settings", "Settings"),
    ]
    if not is_admin:
        nav_items = [
            (href, label)
            for href, label in nav_items
            if href not in {"/users", "/audit"}
        ]
    nav_html = "\n".join(
        f'<a href="{href}" class="{"active" if nav_active == href else ""}"><span>{label}</span></a>'
        for href, label in nav_items
    )

    sidebar_html = ""
    if show_sidebar:
        sidebar_html = f"""
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
      <a href="/logout" class="logout-link">{get_icon('logout')} <span>Logout</span></a>
    </div>
  </div>"""

    layout_class = "layout" if show_sidebar else "layout no-sidebar"

    return HTMLResponse(
        f"""<!DOCTYPE html>
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
    <link rel="stylesheet" href="/static/css/dashboard.css?v=20260517e">
</head>
<body>
<div class="{layout_class}">
  {sidebar_html}
  <div class="main">
    {body}
  </div>
</div>
<script src="/static/js/dashboard.js?v=20260515d"></script>
{extra_js}
</body>
</html>""",
        status_code=status_code,
    )


def api_key_modal(id: str, title: str, body_content: str) -> str:
    return f"""
<div class="modal-overlay" id="{id}" style="display:none">
  <div class="modal">
    <h3>{title}</h3>
    {body_content}
  </div>
</div>"""


class DashboardSearchRequest(BaseModel):
    query: str
    limit: int = 5


def _flatten_text(value) -> list[str]:
    texts: list[str] = []
    if isinstance(value, dict):
        for item in value.values():
            texts.extend(_flatten_text(item))
    elif isinstance(value, list):
        for item in value:
            texts.extend(_flatten_text(item))
    elif value is not None:
        texts.append(str(value))
    return texts


def _query_in_values(query: str, *values) -> bool:
    q = query.lower()
    for value in values:
        for text in _flatten_text(value):
            if q in text.lower():
                return True
    return False


def _json_loads(value):
    if not value:
        return None
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None
    return parsed


def _search_result(kind: str, title: str, summary: str, href: str, meta: str = "") -> dict:
    return {
        "kind": kind,
        "title": title,
        "summary": summary,
        "href": href,
        "meta": meta,
    }


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
            or (activity.get("task_description") or "").startswith("Handoff briefing")
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
    disabled_action_count = sum(
        len(ct.get("disabled_actions") or []) for ct in connector_types
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
        <a class="stat-card stat-link" href="/connectors"><div class="value">{disabled_action_count}</div><div class="label">Disabled Actions</div></a>
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
            f"<td>{str(a.get('updated_at', '') or a.get('started_at', ''))[:16]}</td></tr>"
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
        Agent Core is the local capability layer for your agents. This page gives you a quick read on
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
      <table><thead><tr><th>Task</th><th>Status</th><th>Agent</th><th>Updated</th></tr></thead><tbody>{activity_rows}</tbody></table>
    </div>
    <script>
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


# ─── AGENTS ──────────────────────────────────────────────────────────────────


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
        h += '<label class="checkbox-label" data-scope-row="shared"><input type="checkbox" data-scope="shared"> <span>Shared / global</span></label>'
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
    edit_read_html = build_scope_list("edit-read")
    edit_write_html = build_scope_list("edit-write")

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
            f"<td>{a.get('created_at', '')[:10]}</td>"
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
      setAgentModalReadOnly(Boolean(readOnly));
      // Hide and disable the agent's own scope row: self-access is implicit.
      ['edit-read-scopes', 'edit-write-scopes'].forEach(containerId => {
        document.querySelectorAll('#' + containerId + ' input').forEach(input => {
          const isOwnScope = input.dataset.scope === 'agent:' + a.id;
          input.disabled = Boolean(readOnly) || isOwnScope || input.dataset.requiredScope === 'true';
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
      if (agentModalReadOnly) return;
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

    return render_page(
        "Agents",
        f"""
    <div class="page-header"><h1>Agents</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-agent-modal')">+ Create Agent</button>
    </div></div>
    <div class="card">
      <h3>Agent Access</h3>
      <p class="text-muted access-summary">Agents belong to one owner/default user. Shared/global agents are visible read-only to other users, while edit and key controls stay with the owner or an admin. Use workspaces as shared collaboration spaces; personal user scopes stay tied to the agent owner.</p>
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
            <p class="form-hint">Leave blank unless this agent needs workspace, shared, or agent-private context outside its own private agent scope. Personal user scope access is limited to the owner/default user.</p>
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


# ─── PROJECTS ────────────────────────────────────────────────────────────────


@router.get("/workspaces")
async def workspaces_page(request: Request, session: dict = Depends(require_auth)):
    import json
    from app.services import workspace_service, agent_service

    workspaces = (
        workspace_service.list_workspaces()
        if session.get("role") == "admin"
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )
    all_agents = (
        agent_service.list_agents()
        if session.get("role") == "admin"
        else agent_service.list_agents(owner_user_id=session["user_id"])
    )
    workspace_agent_access_data = [
        {
            "id": a["id"],
            "display_name": a.get("display_name") or a["id"],
            "owner_user_id": a.get("owner_user_id") or "",
            "default_user_id": a.get("default_user_id") or "",
            "is_active": bool(a.get("is_active")),
            "read_scopes": agent_service.parse_scopes(a.get("read_scopes_json", "[]")),
            "write_scopes": agent_service.parse_scopes(a.get("write_scopes_json", "[]")),
        }
        for a in all_agents
    ]

    project_agent_access = {}
    for p in workspaces:
        pscope = f"workspace:{p['id']}"
        read_agents = [
            a["id"]
            for a in all_agents
            if pscope in (json.loads(a.get("read_scopes_json", "[]")) or [])
        ]
        write_agents = [
            a["id"]
            for a in all_agents
            if pscope in (json.loads(a.get("write_scopes_json", "[]")) or [])
        ]
        project_agent_access[p["id"]] = (read_agents, write_agents)

    def workspace_row(p):
        read_tags = "".join(
            f"<span class='scope-tag' title='Read'>{a}</span>"
            for a in project_agent_access[p["id"]][0]
        )
        write_tags = "".join(
            f"<span class='scope-tag scope-write' title='Write'>{a}</span>"
            for a in project_agent_access[p["id"]][1]
        )
        read_tags = read_tags or "<span class='text-muted'>none</span>"
        write_tags = write_tags or "<span class='text-muted'>none</span>"
        is_active = p.get("is_active")
        active_label = "active" if is_active else "inactive"
        if is_active:
            toggle_btn = f"<button type='button' class='btn btn-sm btn-warning' data-workspace-action='deactivate' data-workspace-id='{p['id']}'>Deactivate</button>"
        else:
            toggle_btn = f"<button type='button' class='btn btn-sm btn-secondary' data-workspace-action='reactivate' data-workspace-id='{p['id']}'>Reactivate</button>"
        return (
            f"<tr>"
            f"<td><code>{p['id']}</code></td>"
            f"<td>{p.get('name', '')}</td>"
            f"<td><span class='badge badge-{active_label}'>{active_label}</span></td>"
            f"<td>{p.get('owner_user_id', '')}</td>"
            f"<td class='agent-access-cell'>"
            f"<div class='agent-read-list'><span class='access-label'>Read</span>{read_tags}</div>"
            f"<div class='agent-write-list'><span class='access-label'>Write</span>{write_tags}</div>"
            f"</td>"
            f"<td>{p.get('created_at', '')[:10]}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' data-workspace-action='edit' data-workspace-id='{p['id']}'>Edit</button>"
            f"{toggle_btn}"
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-workspace-action='purge' data-workspace-id='{p['id']}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            f"</div></td></tr>"
        )

    rows = "".join(workspace_row(p) for p in workspaces)

    js = """
    <script>
    const WORKSPACE_AGENT_STATE = Object.fromEntries(
      __WORKSPACE_AGENT_STATE__.map(function(agent) { return [agent.id, agent]; })
    );

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
      openModal('edit-workspace-modal');
      const idEl = document.getElementById('ep-id');
      const nameEl = document.getElementById('ep-name');
      const descriptionEl = document.getElementById('ep-description');
      const collaboratorsEl = document.getElementById('ep-collaborators');
      const agentAccessEl = document.getElementById('ep-agent-access');
      idEl.textContent = id;
      nameEl.value = '';
      descriptionEl.value = '';
      if (collaboratorsEl) {
        collaboratorsEl.innerHTML = '<div class="text-muted">Loading collaborators...</div>';
      }
      if (agentAccessEl) {
        agentAccessEl.innerHTML = '<div class="text-muted">Loading workspace details...</div>';
      }
      try {
        const r = await fetch('/api/workspaces/' + id);
        const j = await r.json();
        if (!j.ok) { throw new Error(j.error?.message || 'Error loading workspace'); }
        const p = j.data.workspace;
        idEl.textContent = p.id;
        nameEl.value = p.name || '';
        descriptionEl.value = p.description || '';
        renderWorkspaceCollaborators(p.id);
        renderWorkspaceAgentAccess(p.id);
      } catch (err) {
        if (collaboratorsEl) {
          collaboratorsEl.innerHTML = '<div class="alert alert-danger">Unable to load collaborators.</div>';
        }
        if (agentAccessEl) {
          agentAccessEl.innerHTML = '<div class="alert alert-danger">Unable to load workspace details.</div>';
        }
        showToast(err.message || 'Error loading workspace', 'danger');
      }
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

    async function renderWorkspaceCollaborators(workspaceId) {
      const container = document.getElementById('ep-collaborators');
      if (!container) return;
      container.innerHTML = '<div class="text-muted">Loading collaborators...</div>';
      try {
        const r = await fetch('/api/workspaces/' + workspaceId + '/collaborators');
        const j = await r.json();
        if (!j.ok) {
          if (r.status === 403) {
            container.innerHTML = '<div class="text-muted">Only the workspace owner or an admin can manage collaborators.</div>';
            return;
          }
          throw new Error(j.error?.message || 'Error loading collaborators');
        }
        const collaborators = j.data?.collaborators || [];
        const rows = collaborators.map(function(c) {
          const canRemove = c.role !== 'owner';
          const removeButton = canRemove
            ? '<button type="button" class="btn btn-sm btn-danger icon-delete-btn" data-workspace-collaborator-remove="true" data-workspace-id="' + escapeHtml(workspaceId) + '" data-user-id="' + escapeHtml(c.user_id || '') + '" title="Remove collaborator" aria-label="Remove collaborator">' + get_icon('delete') + '</button>'
            : '<span class="text-muted">owner</span>';
          return '<tr>' +
            '<td><code>' + escapeHtml(c.user_id || '') + '</code></td>' +
            '<td>' + (c.can_read ? 'Yes' : 'No') + '</td>' +
            '<td>' + (c.can_write ? 'Yes' : 'No') + '</td>' +
            '<td>' + removeButton + '</td>' +
          '</tr>';
        }).join('');
        container.innerHTML =
          '<div class="section-header" style="margin-bottom:8px">' +
            '<h3>Collaborators</h3>' +
            '<div class="section-note">Share this workspace with specific users. Their agents can use workspace scopes while access remains granted.</div>' +
          '</div>' +
          '<div class="card" style="padding:12px;margin-bottom:12px">' +
            '<form data-workspace-collaborator-form="true" data-workspace-id="' + escapeHtml(workspaceId) + '">' +
              '<div class="form-row" style="display:grid;grid-template-columns:1.4fr .7fr .7fr auto;gap:8px;align-items:end">' +
                '<div class="form-group" style="margin:0"><label>User ID</label><input type="text" name="user_id" required placeholder="e.g. brian"></div>' +
                '<label class="checkbox-label" style="margin:0"><input type="checkbox" name="can_read" checked> Read</label>' +
                '<label class="checkbox-label" style="margin:0"><input type="checkbox" name="can_write"> Write</label>' +
                '<button type="submit" class="btn">Add</button>' +
              '</div>' +
            '</form>' +
          '</div>' +
          '<table><thead><tr><th>User</th><th>Read</th><th>Write</th><th class="actions-cell">Actions</th></tr></thead><tbody>' +
            (rows || '<tr><td colspan="4" class="empty">No collaborators yet.</td></tr>') +
          '</tbody></table>';
      } catch (err) {
        container.innerHTML = '<div class="alert alert-danger">' + escapeHtml(err.message || 'Error loading collaborators') + '</div>';
      }
    }

    async function addWorkspaceCollaborator(e, workspaceId) {
      e.preventDefault();
      const form = e.target;
      const data = new FormData(form);
      const userId = (data.get('user_id') || '').toString().trim();
      if (!userId) return false;
      const body = {
        can_read: data.get('can_read') ? true : false,
        can_write: data.get('can_write') ? true : false,
      };
      const j = await apiFetch('/api/workspaces/' + workspaceId + '/collaborators/' + encodeURIComponent(userId), {
        method: 'PUT',
        body: JSON.stringify(body),
      });
      if (j.ok) {
        showToast('Collaborator updated');
        renderWorkspaceCollaborators(workspaceId);
      } else {
        showToast(j.error?.message || 'Failed', 'danger');
      }
      return false;
    }

    async function removeWorkspaceCollaborator(workspaceId, userId) {
      if (!confirm('Remove this collaborator?')) return;
      const j = await apiFetch('/api/workspaces/' + workspaceId + '/collaborators/' + encodeURIComponent(userId), {
        method: 'DELETE',
      });
      if (j.ok) {
        showToast('Collaborator removed');
        renderWorkspaceCollaborators(workspaceId);
      } else {
        showToast(j.error?.message || 'Failed', 'danger');
      }
    }

    document.addEventListener('submit', function(e) {
      const form = e.target;
      if (!(form instanceof HTMLFormElement)) return;
      if (form.dataset.workspaceCollaboratorForm !== 'true') return;
      const workspaceId = form.dataset.workspaceId;
      if (!workspaceId) return;
      addWorkspaceCollaborator(e, workspaceId);
    });

    document.addEventListener('click', function(e) {
      const button = e.target.closest('button[data-workspace-collaborator-remove="true"]');
      if (!button) return;
      const workspaceId = button.dataset.workspaceId;
      const userId = button.dataset.userId;
      if (!workspaceId || !userId) return;
      removeWorkspaceCollaborator(workspaceId, userId);
    });

    function renderWorkspaceAgentAccess(workspaceId) {
      const scope = 'workspace:' + workspaceId;
      const agents = Object.values(WORKSPACE_AGENT_STATE);
      const container = document.getElementById('ep-agent-access');
      if (!container) return;
      container.replaceChildren();

      const header = document.createElement('div');
      header.className = 'section-header';
      header.style.marginBottom = '8px';

      const title = document.createElement('h3');
      title.textContent = 'Agent Access';
      header.appendChild(title);

      const note = document.createElement('div');
      note.className = 'section-note';
      note.textContent = 'This is a convenience editor. Toggling a checkbox updates the selected agent workspace scopes.';
      header.appendChild(note);
      container.appendChild(header);

      const table = document.createElement('table');
      const thead = document.createElement('thead');
      const headRow = document.createElement('tr');
      ['Agent', 'Owner', 'Status', 'Read', 'Write', 'State'].forEach(function(label) {
        const th = document.createElement('th');
        th.textContent = label;
        headRow.appendChild(th);
      });
      thead.appendChild(headRow);
      table.appendChild(thead);

      const tbody = document.createElement('tbody');
      if (!agents.length) {
        const tr = document.createElement('tr');
        const td = document.createElement('td');
        td.colSpan = 6;
        td.className = 'empty';
        td.textContent = 'No agents available.';
        tr.appendChild(td);
        tbody.appendChild(tr);
      } else {
        agents.forEach(function(agent) {
          const readEnabled = (agent.read_scopes || []).includes(scope);
          const writeEnabled = (agent.write_scopes || []).includes(scope);
          const tr = document.createElement('tr');
          tr.dataset.agentId = agent.id;

          const agentTd = document.createElement('td');
          const strong = document.createElement('strong');
          strong.textContent = agent.display_name || agent.id;
          const br = document.createElement('br');
          const code = document.createElement('code');
          code.textContent = agent.id;
          agentTd.appendChild(strong);
          agentTd.appendChild(br);
          agentTd.appendChild(code);
          tr.appendChild(agentTd);

          const ownerTd = document.createElement('td');
          ownerTd.textContent = agent.owner_user_id || '-';
          tr.appendChild(ownerTd);

          const statusTd = document.createElement('td');
          const statusBadge = document.createElement('span');
          statusBadge.className = 'badge ' + (agent.is_active ? 'badge-active' : 'badge-inactive');
          statusBadge.textContent = agent.is_active ? 'active' : 'inactive';
          statusTd.appendChild(statusBadge);
          tr.appendChild(statusTd);

          function makeScopeCell(kind, checked, labelText) {
            const td = document.createElement('td');
            const label = document.createElement('label');
            label.className = 'checkbox-label';
            label.style.margin = '0';
            const input = document.createElement('input');
            input.type = 'checkbox';
            input.dataset.scopeKind = kind;
            input.checked = checked;
            const span = document.createElement('span');
            span.textContent = labelText;
            label.appendChild(input);
            label.appendChild(document.createTextNode(' '));
            label.appendChild(span);
            td.appendChild(label);
            return td;
          }

          tr.appendChild(makeScopeCell('read', readEnabled, 'Read'));
          tr.appendChild(makeScopeCell('write', writeEnabled, 'Write'));

          const stateTd = document.createElement('td');
          stateTd.className = 'agent-scope-status text-muted';
          tr.appendChild(stateTd);

          tbody.appendChild(tr);
        });
      }

      table.appendChild(tbody);
      container.appendChild(table);
    }

    async function toggleWorkspaceAgentScope(agentId, workspaceId, kind, enabled, checkbox) {
      const agent = WORKSPACE_AGENT_STATE[agentId];
      if (!agent) return;
      const scope = 'workspace:' + workspaceId;
      const prior = {
        read: (agent.read_scopes || []).slice(),
        write: (agent.write_scopes || []).slice(),
      };
      const scopes = kind === 'read' ? agent.read_scopes : agent.write_scopes;
      const idx = scopes.indexOf(scope);
      if (enabled && idx === -1) {
        scopes.push(scope);
      } else if (!enabled && idx !== -1) {
        scopes.splice(idx, 1);
      }

      const row = document.querySelector('[data-agent-id="' + CSS.escape(agentId) + '"]');
      const stateCell = row ? row.querySelector('.agent-scope-status') : null;
      const controls = row ? row.querySelectorAll('input[type="checkbox"]') : [];
      controls.forEach(function(input) { input.disabled = true; });
      if (stateCell) stateCell.textContent = 'Saving...';

      const body = {
        read_scopes: agent.read_scopes,
        write_scopes: agent.write_scopes,
      };
      const j = await apiFetch('/api/agents/' + agentId, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) {
        if (stateCell) stateCell.textContent = 'Saved';
        setTimeout(function() {
          if (stateCell && stateCell.textContent === 'Saved') stateCell.textContent = '';
        }, 1000);
      } else {
        agent.read_scopes = prior.read;
        agent.write_scopes = prior.write;
        if (checkbox) checkbox.checked = enabled ? false : true;
        if (stateCell) stateCell.textContent = 'Error';
        showToast(j.error?.message || 'Failed to update agent access', 'danger');
      }
      controls.forEach(function(input) { input.disabled = false; });
    }

    document.addEventListener('change', function(e) {
      const target = e.target;
      if (!(target instanceof HTMLInputElement)) return;
      if (target.type !== 'checkbox') return;
      const scopeKind = target.dataset.scopeKind;
      const row = target.closest('[data-agent-id]');
      if (!scopeKind || !row) return;
      const agentId = row.dataset.agentId;
      const workspaceId = document.getElementById('ep-id').textContent;
      if (!workspaceId) return;
      toggleWorkspaceAgentScope(agentId, workspaceId, scopeKind, target.checked, target);
    });

    document.addEventListener('click', function(e) {
      const button = e.target.closest('button[data-workspace-action]');
      if (!button) return;
      const action = button.dataset.workspaceAction;
      const workspaceId = button.dataset.workspaceId;
      if (!action || !workspaceId) return;
      if (action === 'edit') editProject(workspaceId);
      else if (action === 'deactivate') deactivateProject(workspaceId);
      else if (action === 'reactivate') reactivateProject(workspaceId);
      else if (action === 'purge') purgeProject(workspaceId);
    });
    </script>""".replace(
        "__WORKSPACE_AGENT_STATE__",
        json.dumps(workspace_agent_access_data).replace("</", "<\\/"),
    )
    return render_page(
        "Workspaces",
        f"""
    <div class="page-header"><h1>Workspaces</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-workspace-modal')">+ Create Workspace</button>
    </div></div>
    <div class="card">
      <div class="section-header">
        <h3>Workspaces</h3>
        <div class="section-note">Shared scopes for collaboration. Assign access from Agents → Edit, or use the workspace editor here as a convenience. Scope names stay <code>workspace:&lt;id&gt;</code>.</div>
      </div>
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
            <input type="text" id="cp-id" pattern="[a-z0-9_\\-]+" placeholder="e.g. myproject" required>
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
          <div class="form-group">
            <div id="ep-collaborators"></div>
          </div>
          <div class="form-group">
            <div id="ep-agent-access"></div>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('edit-workspace-modal')">Cancel</button>
            <button type="submit" class="btn">Save</button>
          </div>
        </form>
      </div>
    </div>
    """
        + """
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
    </script>""",
        "/workspaces",
        js,
        session=session,
    )


# ─── USERS ───────────────────────────────────────────────────────────────────


@router.get("/users")
async def users_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import list_users

    if session.get("role") != "admin":
        return render_page(
            "Admin Required",
            """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">Users are managed by administrators.</p>
      <a href="/" class="btn btn-secondary">Back to Overview</a>
    </div>
    """,
            "/",
            session=session,
            status_code=403,
        )

    users = list_users()
    current_user_id = session["user_id"]

    def user_row(u):
        otp = (
            "<span class='badge badge-active'>enrolled</span>"
            if u.get("otp_enrolled")
            else "<span class='badge badge-inactive'>none</span>"
        )
        is_self = u["id"] == current_user_id
        user_payload = escape_html(
            json.dumps(
                {
                    "id": u["id"],
                    "email": u.get("email", ""),
                    "display_name": u.get("display_name", ""),
                    "role": u.get("role", "user"),
                }
            )
        )
        delete_action = (
            "<span class='text-muted' style='font-size:0.8rem'>current session</span>"
            if is_self
            else f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick=\"deleteUser('{u['id']}', '{escape_html(u['display_name'])}')\" title='Delete user' aria-label='Delete user'>{get_icon('delete')}</button>"
        )
        actions = f"<div class='actions-cell'><button type='button' class='btn btn-sm btn-secondary' data-user='{user_payload}' onclick=\"editUser(this)\">Edit</button>{delete_action}</div>"
        return (
            f"<tr>"
            f"<td>{escape_html(u.get('display_name', ''))}</td>"
            f"<td><code>{u['id']}</code></td>"
            f"<td>{escape_html(u.get('email', ''))}</td>"
            f"<td><span class='badge badge-{'active' if u.get('role') == 'admin' else 'inactive'}'>{u.get('role', 'user')}</span></td>"
            f"<td>{otp}</td>"
            f"<td>{u.get('created_at', '')[:10]}</td>"
            f"<td>{actions}</td>"
            f"</tr>"
        )

    rows = (
        "".join(user_row(u) for u in users)
        or "<tr><td colspan=7 class=empty>No users.</td></tr>"
    )

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

    return render_page(
        "Users",
        f"""
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
            <div class="form-group"><label>Initial Password</label><input type="password" name="password" minlength="8" required></div>
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
    """,
        "/users",
        js,
        session=session,
    )


# ─── MEMORY ──────────────────────────────────────────────────────────────────


@router.get("/memory")
async def memory_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import memory_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service
    from app.database import get_db

    is_admin = session.get("role") == "admin"
    agents = (
        list_agents()
        if session.get("role") == "admin"
        else list_agents(owner_user_id=session["user_id"])
    )
    agent_options = "".join(
        f'<option value="agent:{a["id"]}">Agent: {escape_html(a.get("display_name") or a["id"])} (agent:{a["id"]})</option>'
        for a in agents
    )

    workspaces = (
        workspace_service.list_workspaces()
        if session.get("role") == "admin"
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )
    project_options = "".join(
        f'<option value="workspace:{p["id"]}">Workspace: {escape_html(p.get("name") or p["id"])} (workspace:{p["id"]})</option>'
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
            records.extend(
                memory_service.get_memory_by_scope(
                    scope=scope, limit=200, record_status=record_status
                )
                or []
            )
        records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        return records[:200]

    active_records = list_visible_memory("active")
    retracted_records = list_visible_memory("retracted")

    def active_row(r):
        return (
            f"<tr><td><span class='badge badge-{r.get('memory_class', '')}'>{r.get('memory_class', '')}</span></td>"
            f"<td>{escape_html(r.get('content', '')[:80])}</td>"
            f"<td><code>{(r.get('scope') or '').replace('workspace:', '')}</code></td>"
            f"<td>{r.get('domain', '') or ''}</td>"
            f"<td>{r.get('confidence', 0.5):.1f}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"viewMemory('{r['id']}')\">Detail</button>"
            f"<button type='button' class='btn btn-sm btn-warning' onclick=\"retractRecord('{r['id']}')\">Retract</button>"
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick=\"deleteRecord('{r['id']}')\" title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            f"</div></td></tr>"
        )

    def retracted_row(r):
        return (
            f"<tr style='opacity:0.65'><td><span class='badge badge-inactive'>{r.get('memory_class', '')}</span></td>"
            f"<td>{escape_html(r.get('content', '')[:80])}</td>"
            f"<td><code>{(r.get('scope') or '').replace('workspace:', '')}</code></td>"
            f"<td>{r.get('domain', '') or ''}</td>"
            f"<td>{r.get('confidence', 0.5):.1f}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"restoreRecord('{r['id']}')\">Restore</button>"
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' onclick=\"deleteRecord('{r['id']}')\" title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            f"</div></td></tr>"
        )

    records_rows = (
        "".join(active_row(r) for r in active_records)
        or "<tr><td colspan=6 class=empty>No active records.</td></tr>"
    )
    retracted_rows = "".join(retracted_row(r) for r in retracted_records)

    js = (
        """
    <script>
    async function refreshMemory() { setTimeout(() => location.reload(), 150); }
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
      document.getElementById('mem-detail-scope').textContent = (r.scope || '').replace('workspace:', '');
      document.getElementById('mem-detail-domain').textContent = r.domain || '';
      document.getElementById('mem-detail-topic').textContent = r.topic || '';
      document.getElementById('mem-detail-confidence').textContent = r.confidence != null ? r.confidence.toFixed(2) : '';
      document.getElementById('mem-detail-importance').textContent = r.importance != null ? r.importance.toFixed(2) : '';
      document.getElementById('mem-detail-created').textContent = r.created_at ? r.created_at.substring(0, 19) : '';
      document.getElementById('mem-detail-status').textContent = r.record_status || '';
      document.getElementById('mem-detail-slot-key').textContent = r.slot_key || '';
      document.getElementById('mem-detail-valid-from').textContent = r.valid_from ? r.valid_from.substring(0, 19) : '';
      document.getElementById('mem-detail-valid-to').textContent = r.valid_to ? r.valid_to.substring(0, 19) : '';
      document.getElementById('mem-detail-last-confirmed').textContent = r.last_confirmed_at ? r.last_confirmed_at.substring(0, 19) : '';
      const provenanceEl = document.getElementById('mem-detail-provenance');
      if (r.provenance_json) {
        try {
          provenanceEl.textContent = JSON.stringify(JSON.parse(r.provenance_json), null, 2);
        } catch (err) {
          provenanceEl.textContent = r.provenance_json;
        }
        provenanceEl.style.display = 'block';
      } else {
        provenanceEl.textContent = '';
        provenanceEl.style.display = 'none';
      }
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
      if (!chain.length) {
        el.innerHTML = '<div class="text-muted">No history yet. This record has not been replaced by a newer version.</div>';
        document.getElementById('mem-detail-chain').style.display = 'block';
        return;
      }
      el.innerHTML = chain.map((r, i) => {
        const edge = i > 0 ? '<div class="text-muted" style="font-size:0.8rem;margin-bottom:2px">&lt;- earlier version</div>' : '';
        const tag = i === chain.length - 1 ? 'Current' : 'Earlier';
        const metadataParts = [];
        if (r.domain) metadataParts.push(r.domain);
        if (r.topic) metadataParts.push(r.topic);
        if (r.record_status) metadataParts.push(r.record_status);
        if (r.created_at) metadataParts.push((r.created_at || '').substring(0, 19));
        const metadata = metadataParts.length ? metadataParts.map(escapeHtml).join(' · ') : '';
        return '<div style="margin:8px 0;padding:8px 10px;border:1px solid var(--border);border-radius:8px;background:var(--bg);white-space:normal;overflow-wrap:anywhere">' +
          edge +
          '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:4px">' +
          '<span class="badge badge-' + (r.memory_class || '') + '">' + (r.memory_class || '') + '</span>' +
          '<span class="badge">' + tag + '</span>' +
          '<span class="text-muted" style="font-size:0.8rem">' + metadata + '</span>' +
          '</div>' +
          '<div style="font-size:0.9rem;line-height:1.35">' + escapeHtml(r.content || '') + '</div>' +
          '</div>';
      }).join('');
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
      if (e && e.preventDefault) e.preventDefault();
      try {
        const body = {
	          content: document.getElementById('mem-content').value,
	          memory_class: document.getElementById('mem-write-class').value,
	          scope: document.getElementById('mem-write-scope').value || '"""
        + user_scope
        + """',
	          domain: document.getElementById('mem-domain').value || null,
	          topic: document.getElementById('mem-topic').value || null,
	          confidence: parseFloat(document.getElementById('mem-confidence').value) || 0.5,
	          importance: parseFloat(document.getElementById('mem-importance').value) || 0.5,
	          source_kind: 'operator_authored',
	          slot_key: document.getElementById('mem-slot-key').value.trim() || null,
	          valid_from: document.getElementById('mem-valid-from').value || null,
	          valid_to: document.getElementById('mem-valid-to').value || null,
	          last_confirmed_at: document.getElementById('mem-last-confirmed').value || null,
	        };
        const j = await apiFetch('/api/memory/write', { method: 'POST', body: JSON.stringify(body) });
        if (j.ok) { showToast('Written'); closeModal('write-memory-modal'); refreshMemory(); }
        else { showToast(j.error.message || 'Failed', 'danger'); }
      } catch (err) {
        console.error('Memory write failed', err);
        showToast('Memory write failed: ' + (err.message || err), 'danger');
      }
    }
    function displayRecords(records) {
      const tbody = document.getElementById('mem-results-body');
      if (!records.length) { tbody.innerHTML = '<tr><td colspan=6 class=empty>No records found.</td></tr>'; return; }
      tbody.innerHTML = records.map(r => `
        <tr>
          <td><span class="badge badge-${r.memory_class}">${r.memory_class}</span></td>
          <td>${escapeHtml(r.content || '').substring(0, 80)}</td>
          <td><code>${(r.scope || '').replace('workspace:', '')}</code></td>
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
    )

    return render_page(
        "Memory",
        _hf(
            f"""
    <div class="page-header"><h1>Memory</h1><div class="page-actions">
        <button class="btn" onclick="openModal('write-memory-modal')">+ Save Memory</button>
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
        <button class="btn" onclick="doSearch()">Search</button>
      </div>
      <details style="margin-top:10px">
        <summary class="text-muted" style="cursor:pointer">Advanced search filters</summary>
        <div class="filter-bar" style="margin-top:10px">
	        <select id="mem-class" title="Memory class filter">
	          <option value="">All memory classes</option>
	          <option value="fact">fact</option>
	          <option value="preference">preference</option>
	          <option value="decision">decision</option>
	          <option value="scratchpad">scratchpad</option>
	        </select>
          <input type="text" id="mem-search-domain" placeholder="Domain, e.g. engineering">
          <input type="text" id="mem-search-topic" placeholder="Topic, e.g. docker">
          <input type="number" id="mem-min-confidence" placeholder="Min confidence" min="0" max="1" step="0.1">
        </div>
        <p class="form-hint">Use these only when you want to narrow the result set. Search always respects scope permissions first.</p>
      </details>
    </div>

    <!-- Active Records -->
    <div class="card">
      <h3>Active Records <span id="mem-count" class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(active_records)})</span></h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">These are the active records you can read right now. Use Search for a narrower view.</p>
      <table><thead><tr><th>Class</th><th>Content</th><th>Scope</th><th>Domain</th><th>Confidence</th><th>Actions</th></tr></thead>
      <tbody id="mem-results-body">
        {records_rows}
      </tbody>
      <input type="hidden" id="current-scope" value="{user_scope}">
    </div>

    <!-- Retracted Records -->
    """
            + (
                f"""
    <div class="card" style="border-left:4px solid var(--text-muted)">
      <h3 style="color:var(--text-muted)">Retracted Records <span class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(retracted_records)})</span></h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">These records are hidden from search. Restore to make them active again, or permanently delete.</p>
      <table><thead><tr><th>Class</th><th>Content</th><th>Scope</th><th>Domain</th><th>Confidence</th><th>Actions</th></tr></thead>
      <tbody>{retracted_rows or "<tr><td colspan=6 class=empty>No retracted records.</td></tr>"}</tbody></table>
    </div>
    """
                if retracted_records
                else ""
            )
            + f"""

    <!-- Write Memory Modal -->
    <div class="modal-overlay" id="write-memory-modal" style="display:none">
      <div class="modal">
        <h3>Save Memory</h3>
        <form id="write-memory-form" onsubmit="doWrite(event)">
          <div class="form-group">
            <label>What should be remembered? *</label>
            <textarea id="mem-content" rows="3" required placeholder="A concise fact, preference, decision, or scratchpad note"></textarea>
            <p class="form-hint">This is the text future agents will retrieve. Keep it short and specific.</p>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Save as *</label>
              <select id="mem-write-class" required>
                <option value="fact">Fact</option>
                <option value="decision">Decision</option>
                <option value="preference">Preference</option>
                <option value="scratchpad">Scratchpad</option>
              </select>
              <p class="form-hint">Fact is the safe default. Use decision for chosen direction, preference for user/team preferences, and scratchpad for temporary notes.</p>
            </div>
            <div class="form-group">
              <label>Save to *</label>
              <select id="mem-write-scope" required>
                <option value="{user_scope}" selected>{user_scope_label}</option>
                {agent_options}
                {project_options}
                <option value="shared">Shared memory (shared)</option>
              </select>
              <p class="form-hint">Personal memory is the default. Workspace and agent scopes are for shared team or agent-private context. Shared memory is PII-checked.</p>
            </div>
          </div>
          <details style="margin-top:4px">
            <summary class="text-muted" style="cursor:pointer">More options</summary>
            <div class="form-row" style="margin-top:10px">
              <div class="form-group">
                <label>Domain</label>
                <input type="text" id="mem-domain" placeholder="e.g. coding">
                <p class="form-hint">Optional tag for searches and filtering.</p>
              </div>
              <div class="form-group">
                <label>Topic</label>
                <input type="text" id="mem-topic" placeholder="e.g. style">
                <p class="form-hint">Optional tag for searches and filtering.</p>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label>Confidence</label>
                <input type="number" id="mem-confidence" value="1" min="0" max="1" step="0.1">
                <p class="form-hint">Lower values can be filtered out during search.</p>
              </div>
              <div class="form-group">
                <label>Importance</label>
                <input type="number" id="mem-importance" value="0.7" min="0" max="1" step="0.1">
                <p class="form-hint">Higher values rank earlier when results are similar.</p>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label>Preference Slot Key</label>
                <input type="text" id="mem-slot-key" placeholder="e.g. style">
                <p class="form-hint">Optional. Use for preferences when you want one active value per slot.</p>
              </div>
              <div class="form-group">
                <label>Last Confirmed At</label>
                <input type="datetime-local" id="mem-last-confirmed">
                <p class="form-hint">Optional freshness hint for the latest confirmation time.</p>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label>Valid From</label>
                <input type="datetime-local" id="mem-valid-from">
                <p class="form-hint">Optional start time for the record's usefulness window.</p>
              </div>
              <div class="form-group">
                <label>Valid To</label>
                <input type="datetime-local" id="mem-valid-to">
                <p class="form-hint">Optional end time for the record's usefulness window.</p>
              </div>
            </div>
          </details>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('write-memory-modal')">Cancel</button>
            <button type="submit" class="btn" onclick="doWrite(event)">Write</button>
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
        <div class="form-row">
          <div class="form-group"><label>Status</label><span id="mem-detail-status" class="badge"></span></div>
          <div class="form-group"><label>Created</label><span id="mem-detail-created" class="text-muted"></span></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Slot Key</label><code id="mem-detail-slot-key"></code></div>
          <div class="form-group"><label>Last Confirmed At</label><span id="mem-detail-last-confirmed" class="text-muted"></span></div>
        </div>
        <div class="form-row">
          <div class="form-group"><label>Valid From</label><span id="mem-detail-valid-from" class="text-muted"></span></div>
          <div class="form-group"><label>Valid To</label><span id="mem-detail-valid-to" class="text-muted"></span></div>
        </div>
        <div class="form-group">
          <label>Provenance</label>
          <pre id="mem-detail-provenance" style="display:none;background:var(--bg);padding:8px;border-radius:6px;white-space:pre-wrap;max-height:180px;overflow:auto"></pre>
        </div>
        <input type="hidden" id="mem-detail-id" value="">
        <div id="mem-detail-supersede" class="alert alert-warning" style="display:none"></div>
        <div id="mem-detail-chain" style="display:none">
          <h4 style="margin-top:12px">Memory History</h4>
          <p class="text-muted" style="margin:4px 0 10px;font-size:0.85rem">Shows the current record and any earlier versions it replaced.</p>
          <div id="mem-chain-content" class="text-muted" style="font-size:0.85rem;max-height:220px;overflow:auto"></div>
        </div>
        <div class="modal-footer">
          <button class="btn btn-sm btn-secondary" onclick="showChain()">Show Memory History</button>
          <button class="btn btn-secondary" onclick="closeModal('memory-detail-modal')">Close</button>
        </div>
      </div>
    </div>
    """
        ),
        "/memory",
        js,
        session=session,
    )


# ─── ACTIVITY ────────────────────────────────────────────────────────────────


@router.get("/activity")
async def activity_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import activity_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"
    activities = (
        activity_service.list_activities(
            user_id=None if is_admin else session["user_id"],
            limit=100,
        )
        or []
    )
    all_agents = (
        list_agents() if is_admin else list_agents(owner_user_id=session["user_id"])
    )
    agent_labels = {
        a["id"]: a.get("display_name") or a["id"] for a in all_agents
    }
    agent_options = "".join(
        f'<option value="{a["id"]}">{a.get("display_name", a["id"])}</option>'
        for a in all_agents
    )
    workspaces = (
        workspace_service.list_workspaces()
        if is_admin
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )
    user_scope = f"user:{session['user_id']}"
    activity_scope_options = (
        f'<option value="{user_scope}">Personal user memory ({user_scope})</option>'
        + "".join(
            f'<option value="workspace:{p["id"]}">Workspace: {escape_html(p.get("name") or p["id"])} (workspace:{p["id"]})</option>'
            for p in workspaces
        )
        + "".join(
            f'<option value="agent:{a["id"]}">Agent private: {escape_html(a.get("display_name") or a["id"])} (agent:{a["id"]})</option>'
            for a in all_agents
        )
    )
    # For reassign modal, we want the same options
    reassign_options = agent_options
    coordination_activities = [
        a for a in activities if a.get("status") in ("active", "stale", "blocked")
    ]
    attention_activities = [
        a for a in activities if a.get("status") in ("stale", "blocked")
    ]
    owner_counts = Counter(
        a.get("assigned_agent_id") or a.get("agent_id") or "unassigned"
        for a in coordination_activities
    )
    handoff_count = sum(1 for a in activities if a.get("reassigned_from_agent_id"))
    active_owner_count = len(owner_counts)
    coordination_summary = "".join(
        f"<span class='badge badge-active' style='margin-right:8px;margin-bottom:8px'>{escape_html(agent_labels.get(agent_id, agent_id))}: {count}</span>"
        for agent_id, count in owner_counts.most_common(6)
    )

    status_filters = [
        "active",
        "stale",
        "reassigned",
        "completed",
        "blocked",
        "cancelled",
    ]
    status_tabs = "".join(
        f"<button class='btn btn-sm {'btn' if i == 0 else 'btn-secondary'} status-filter' data-status='{s}' onclick='filterActivity(\"{s}\",this)'>{s.title()}</button>"
        for i, s in enumerate(status_filters)
    )

    rows = "".join(
        f"<tr class='activity-row' data-status='{a.get('status', '')}'>"
        f"<td class='activity-id-cell'><code>{a.get('id', '')[:12]}</code></td>"
        f"<td class='activity-task-cell'>{a.get('task_description', '')[:60]}</td>"
        f"<td class='activity-scope-cell'><code>{escape_html((a.get('memory_scope') or '').replace('workspace:', '')) or '—'}</code></td>"
        f"<td class='activity-status-cell'><span class='badge badge-{a.get('status', 'active')}'>{a.get('status', '')}</span></td>"
        f"<td class='activity-agent-cell'>{escape_html(agent_labels.get(a.get('assigned_agent_id', ''), a.get('assigned_agent_id', '')))}</td>"
        f"<td class='activity-handoff-cell'>{escape_html(agent_labels.get(a.get('reassigned_from_agent_id', ''), a.get('reassigned_from_agent_id', '')))}</td>"
        f"<td class='activity-updated-cell'>{str(a.get('updated_at', ''))[:16]}</td>"
        f"<td><div class='actions-cell activity-actions-cell'>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"createHandoff('{a['id']}')\">Briefing</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"reassignActivity('{a['id']}')\" title='Reassign'>Reassign</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"updateActivity('{a['id']}','active')\" {'disabled' if a.get('status') not in ('stale', 'blocked', 'reassigned') else ''} title='Reactivate'>Start</button>"
        f"<button type='button' class='btn btn-sm btn-secondary' onclick=\"updateActivity('{a['id']}','completed')\" {'disabled' if a.get('status') not in ('active', 'stale', 'blocked', 'reassigned') else ''} title='Complete'>Done</button>"
        f"<button type='button' class='btn btn-sm btn-danger' onclick=\"cancelActivity('{a['id']}')\" {'disabled' if a.get('status') not in ('active', 'stale', 'blocked', 'reassigned') else ''} title='Cancel'>Cancel</button>"
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
        const section = function(title, items, emptyLabel) {
          const rows = (items || []).map(function(item) {
            const label = item.content || item.description || item.task_description || '';
            const meta = item.ended_at || item.started_at || item.generated_at || item.outcome || '';
            return '<li><div style="font-weight:600">' + escapeHtml(label).substring(0, 120) + '</div>' +
              (meta ? '<div class="text-muted" style="font-size:0.85rem">' + escapeHtml(String(meta)).substring(0, 60) + '</div>' : '') +
              '</li>';
          }).join('');
          return '<div style="margin-top:12px"><h4 style="margin-bottom:8px">' + escapeHtml(title) + '</h4>' +
            '<ul style="margin:0;padding-left:20px">' + (rows || '<li class="text-muted">' + escapeHtml(emptyLabel) + '</li>') + '</ul></div>';
        };
        const meta = [
          '<div class="card" style="margin-bottom:12px;padding:12px">',
          '<div class="text-muted" style="font-size:0.85rem">Source activity</div>',
          '<div style="font-weight:600">' + escapeHtml(b.task_description || '') + '</div>',
          '<div class="text-muted" style="font-size:0.85rem;margin-top:4px">',
          'Agent: ' + escapeHtml(b.agent_id || '') + ' | Assigned: ' + escapeHtml(b.assigned_agent_id || '') + ' | Generated: ' + escapeHtml((b.generated_at || '').substring(0, 19)),
          '</div>',
          '</div>'
        ].join('');
        document.getElementById('briefing-content').innerHTML =
          meta +
          section('Facts', b.facts, 'No facts') +
          section('Decisions', b.decisions, 'No decisions') +
          section('Preferences', b.preferences, 'No preferences') +
          section('Recent Completed', b.recent_completed, 'No recent completed activity');
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

    return render_page(
        "Activity",
        f"""
    <div class="page-header"><h1>Activity</h1><div class="page-actions">
        <button class="btn" onclick="openModal('create-activity-modal')">+ New Activity</button>
    </div></div>
    <div class="card">
      <h3>Tasks</h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Activities track current work, handoff briefings, and stale tasks. Keep the task description short; the default memory scope is the selected agent's private scope. Use reassign and briefing when work needs to move between agents, users, or workspaces.</p>
      <div class="card" style="margin-bottom:12px;padding:14px">
        <div class="section-header" style="margin-bottom:8px">
          <h3>Coordination Snapshot</h3>
          <div class="section-note">Who owns work right now and where handoffs have happened.</div>
        </div>
        <div class="stat-grid" style="margin-bottom:10px">
          <div class="stat-card"><div class="value">{len(coordination_activities)}</div><div class="label">Open Work Items</div></div>
          <div class="stat-card"><div class="value">{active_owner_count}</div><div class="label">Assigned Agents</div></div>
          <div class="stat-card"><div class="value">{handoff_count}</div><div class="label">Recent Handoffs</div></div>
          <div class="stat-card"><div class="value">{len(attention_activities)}</div><div class="label">Needs Attention</div></div>
        </div>
        <div class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Current ownership:</div>
        <div>{coordination_summary or "<span class='text-muted'>No open work items yet.</span>"}</div>
      </div>
      <div class="filter-bar" style="margin-bottom:12px">
        <button class='btn btn-sm status-filter' data-status='all' onclick='filterActivity("all",this)'>All</button>
        {status_tabs}
      </div>
      <div style="overflow-x:auto">
      <table class="activity-table"><thead><tr><th class="activity-id-cell">ID</th><th class="activity-task-cell">Task</th><th class="activity-scope-cell">Scope</th><th class="activity-status-cell">Status</th><th class="activity-agent-cell">Agent</th><th class="activity-handoff-cell">Handoff From</th><th class="activity-updated-cell">Updated</th><th class="activity-actions-header">Actions</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=8 class=empty>No activities yet.</td></tr>"}</tbody></table>
      </div>
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
            <p class="form-hint">This agent will own the activity and receive heartbeats.</p>
          </div>
          <div class="form-group">
            <label>Task Description *</label>
            <textarea id="act-task" rows="3" required placeholder="What should the agent do?"></textarea>
            <p class="form-hint">Write the task the same way you would hand it to a person.</p>
          </div>
          <details style="margin-top:4px">
            <summary class="text-muted" style="cursor:pointer">Advanced options</summary>
            <div class="form-group" style="margin-top:10px">
              <label>Memory Scope</label>
              <select id="act-memory-scope">
                <option value="">Use agent private scope (recommended)</option>
                {activity_scope_options}
              </select>
              <p class="form-hint">Leave this on the default unless you want the activity to write into a workspace or personal user scope instead.</p>
            </div>
          </details>
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
    """,
        "/activity",
        js,
        session=session,
    )


# ─── AUDIT ────────────────────────────────────────────────────────────────────


@router.get("/audit")
async def audit_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import audit_service

    if session.get("role") != "admin":
        return render_page(
            "Admin Required",
            """
    <div class="page-header"><h1>Admin Access Required</h1></div>
    <div class="card">
      <p class="text-muted">The audit log is available to administrators only.</p>
      <a href="/" class="btn btn-secondary">Back to Overview</a>
    </div>
    """,
            "/",
            session=session,
            status_code=403,
        )

    page = int(request.query_params.get("page", 1))
    limit = 50
    offset = (page - 1) * limit

    actor_filter = request.query_params.get("actor_type", "")
    action_filter = request.query_params.get("action", "")
    resource_filter = request.query_params.get("resource_type", "")
    result_filter = request.query_params.get("result", "")

    from app.services.audit_service import ACTOR_TYPES, RESULT_TYPES, AUDIT_ACTIONS

    all_events = (
        audit_service.query_events(
            actor_type=actor_filter or None,
            action=action_filter or None,
            resource_type=resource_filter or None,
            result=result_filter or None,
            limit=limit,
            offset=offset,
        )
        or []
    )
    total_events = len(all_events)
    success_count = sum(1 for e in all_events if e.get("result") == "success")
    failure_count = sum(1 for e in all_events if e.get("result") == "failure")
    blocked_count = sum(1 for e in all_events if e.get("result") == "blocked")
    events_with_details = sum(1 for e in all_events if e.get("details_json"))
    actor_counts = Counter(e.get("actor_type") or "unknown" for e in all_events)
    action_counts = Counter(e.get("action") or "unknown" for e in all_events)
    resource_counts = Counter(
        e.get("resource_type") or "-" for e in all_events if e.get("resource_type")
    )

    def details_preview(event: dict) -> str:
        raw = event.get("details_json")
        if not raw:
            return ""
        try:
            parsed = json.loads(raw)
        except Exception:
            return ""
        if not isinstance(parsed, dict) or not parsed:
            return ""
        preview_items = []
        for key, value in list(parsed.items())[:2]:
            if isinstance(value, (dict, list)):
                preview_items.append(f"{key}=" + json.dumps(value)[:40])
            else:
                preview_items.append(f"{key}={str(value)[:40]}")
        return escape_html(" · ".join(preview_items))

    no_details_html = "<span class='text-muted'>No details</span>"

    top_actor_summary = "".join(
        f"<span class='badge badge-active' style='margin-right:8px;margin-bottom:8px'>{escape_html(actor)}: {count}</span>"
        for actor, count in actor_counts.most_common(4)
    )
    top_action_summary = "".join(
        f"<span class='badge badge-secondary' style='margin-right:8px;margin-bottom:8px'>{escape_html(action)}: {count}</span>"
        for action, count in action_counts.most_common(4)
    )
    top_resource_summary = "".join(
        f"<span class='badge badge-stale' style='margin-right:8px;margin-bottom:8px'>{escape_html(resource)}: {count}</span>"
        for resource, count in resource_counts.most_common(4)
    )

    def audit_row(event: dict) -> str:
        result_class = "active" if event.get("result", "") == "success" else "cancelled"
        resource_id = event.get("resource_id", "")
        details_html = details_preview(event) or no_details_html
        return (
            f"<tr><td class='audit-time-cell'>{event.get('timestamp', '')[:19]}</td>"
            f"<td class='audit-actor-cell'><span class='badge badge-secondary'>{event.get('actor_type', '')}</span></td>"
            f"<td class='audit-action-cell'><code>{event.get('action', '')}</code></td>"
            f"<td class='audit-resource-cell'>{event.get('resource_type', '') or '-'}</td>"
            f"<td class='audit-resource-id-cell'><code>{resource_id[:14] if resource_id else '-'}</code></td>"
            f"<td class='audit-result-cell'><span class='badge badge-{result_class}'>{event.get('result', '')}</span></td>"
            f"<td class='mono audit-details-cell'>{details_html}</td>"
            f"<td class='mono audit-ip-cell'>{event.get('ip_address', '') or '-'}</td></tr>"
        )

    rows = "".join(audit_row(e) for e in all_events)

    def build_options(items, selected):
        return "".join(
            f"<option value='{i}' {'selected' if i == selected else ''}>{i}</option>"
            for i in items
        )

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

    return render_page(
        "Audit Log",
        f"""
    <div class="page-header"><h1>Audit Log</h1><div class="page-actions">
        <button class="btn btn-secondary" onclick="exportAuditCsv()">Export CSV</button>
    </div></div>
    <div class="card">
      <h3>Events</h3>
      <div class="card" style="margin-bottom:12px;padding:14px">
        <div class="section-header" style="margin-bottom:8px">
          <h3>Audit Snapshot</h3>
          <div class="section-note">A quick read on how much is happening and what kinds of events are being written.</div>
        </div>
        <div class="stat-grid" style="margin-bottom:10px">
          <div class="stat-card"><div class="value">{total_events}</div><div class="label">Events on Page</div></div>
          <div class="stat-card"><div class="value">{success_count}</div><div class="label">Success</div></div>
          <div class="stat-card"><div class="value">{failure_count}</div><div class="label">Failure</div></div>
          <div class="stat-card"><div class="value">{blocked_count}</div><div class="label">Blocked</div></div>
          <div class="stat-card"><div class="value">{events_with_details}</div><div class="label">With Details</div></div>
        </div>
        <div class="text-muted" style="font-size:0.85rem;margin-bottom:8px">Top actors:</div>
        <div>{top_actor_summary or "<span class='text-muted'>No events yet.</span>"}</div>
        <div class="text-muted" style="font-size:0.85rem;margin:12px 0 8px">Top actions:</div>
        <div>{top_action_summary or "<span class='text-muted'>No events yet.</span>"}</div>
        <div class="text-muted" style="font-size:0.85rem;margin:12px 0 8px">Top resources:</div>
        <div>{top_resource_summary or "<span class='text-muted'>No events yet.</span>"}</div>
      </div>
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
          <option value="agent" {"selected" if resource_filter == "agent" else ""}>agent</option>
          <option value="workspace" {"selected" if resource_filter == "workspace" else ""}>workspace</option>
          <option value="memory" {"selected" if resource_filter == "memory" else ""}>memory</option>
          <option value="credential" {"selected" if resource_filter == "credential" else ""}>credential</option>
          <option value="activity" {"selected" if resource_filter == "activity" else ""}>activity</option>
        </select>
        <select id="audit-result" style="width:120px">
          <option value="">Any result</option>
          {result_options}
        </select>
        <button class="btn btn-sm" onclick="applyAuditFilters()">Filter</button>
        <button class="btn btn-sm btn-secondary" onclick="clearAuditFilters()">Clear</button>
      </div>
      <div style="overflow-x:auto">
      <table class="audit-table"><thead><tr><th class="audit-time-cell">Time</th><th class="audit-actor-cell">Actor Type</th><th class="audit-action-cell">Action</th><th class="audit-resource-cell">Resource</th><th class="audit-resource-id-cell">Resource ID</th><th class="audit-result-cell">Result</th><th>Details</th><th class="audit-ip-cell">IP</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=8 class=empty>No events yet.</td></tr>"}</tbody></table>
      </div>
      <div class="pagination" style="margin-top:12px;display:flex;gap:8px;align-items:center">
        <a href="?page={prev_page}{f"&actor_type={actor_filter}" if actor_filter else ""}{f"&action={action_filter}" if action_filter else ""}{f"&resource_type={resource_filter}" if resource_filter else ""}{f"&result={result_filter}" if result_filter else ""}" class="btn btn-sm btn-secondary">Prev</a>
        <span>{page_info}</span>
        <a href="?page={next_page}{f"&actor_type={actor_filter}" if actor_filter else ""}{f"&action={action_filter}" if action_filter else ""}{f"&resource_type={resource_filter}" if resource_filter else ""}{f"&result={result_filter}" if result_filter else ""}" class="btn btn-sm btn-secondary">Next</a>
      </div>
    </div>
    """,
        "/audit",
        js,
        session=session,
    )


# ─── SETTINGS ─────────────────────────────────────────────────────────────────


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
                f"<tr><td>Broker Credential</td><td><span class='badge badge-{broker_badge}'>{broker_label}</span></td><td class='text-muted'>Resolves AC_SECRET_* references at runtime</td></tr>",
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
        backup_html = """
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
      <p class="form-hint">Maintenance marks stale activities using <code>AGENT_CORE_STALE_THRESHOLD_MINUTES</code> and deletes active scratchpad memories older than the retention setting below. This does not touch credentials or connector bindings.</p>
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
      <p class="text-muted" style="margin-bottom:12px">The broker credential resolves <code>AC_SECRET_*</code> references at runtime. Rotate it if a local consumer should stop using the current broker secret.</p>
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
    async function exportBackup() {{
      const r = await fetch('/api/backup/export', {{
        method: 'POST',
      }});
      if (r.ok) {{
        const blob = await r.blob();
        const backupKey = r.headers.get('X-Agent-Core-Backup-Key');
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = 'agent-core-backup.zip.enc'; a.click();
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
        ("instructions", "Instructions", "agent-core-instructions.md"),
        ("mcp_json", "MCP Config", "agent-core-mcp-config.txt"),
        ("env", "Environment Variables", "agent-core.env"),
        ("claude_md", "CLAUDE.md", "CLAUDE.md"),
        ("agents_md", "AGENTS.md", "AGENTS.md"),
        ("session", "Session Prompt", "agent-core-session-prompt.md"),
        ("verification", "Verification Prompt", "agent-core-verification.md"),
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
        "agent-core-output.txt",
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
        "agent-core-output.txt",
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
        )
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
      <p class="subtitle">Generate setup instructions, environment variables, MCP config, and AI-facing prompts for connecting tools to Agent Core.</p>
      <div class="text-muted" style="font-size:0.86rem;margin-top:8px">
        Current tool preset: <strong>{escape_html(tool_label)}</strong>. The main presets are Claude Code, Codex, Cursor, Windsurf, Antigravity, and Generic MCP/REST. The generated outputs in this page are the canonical setup files and prompts.
      </div>
    </div>

    <form method="get" action="{page_path}#generated-output" class="setup-form">
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
        tool_tips = """## Claude Code Tips

- Claude Code automatically reads `CLAUDE.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Claude Code will inherit the scopes from your Agent Core agent configuration. The scopes above are informational.
- For best results, set `AGENT_CORE_API_KEY` in your shell environment before starting Claude Code:
    export AGENT_CORE_API_KEY="your-key-here"
"""
    elif target == "codex":
        scope_guide = f"- Work in `{default_scope}` for default context.\n- Read `{user_scope}` for user preferences.\n- Keep private notes in `{agent_scope}`."
        tool_tips = """## Codex Tips

- Codex reads `AGENTS.md` in the workspace root. Consider generating that output instead for a self-contained file.
- Codex can use both the MCP tools and the REST API. The MCP endpoint is preferred for memory operations.
- Set `AGENT_CORE_API_KEY` in your environment before starting a Codex session.
"""
    elif target == "cursor":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = """## Cursor Tips

- Add the MCP config to `.cursor/mcp.json` in the workspace root for workspace-level access, or `~/.cursor/mcp.json` for global access.
- After adding the MCP config, run the "Reload MCP Servers" command or restart Cursor.
- Cursor's AI chat can use MCP tools directly once the server is connected. Set `AGENT_CORE_API_KEY` in Cursor's terminal or your shell profile.
"""
    elif target == "windsurf":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = """## Windsurf Tips

- Add the MCP config to Windsurf's MCP settings for your workspace.
- Windsurf may require a restart after adding MCP servers.
- Set `AGENT_CORE_API_KEY` in your shell environment before starting a Windsurf session.
"""
    elif target == "antigravity":
        scope_guide = f"{workspace_context_line}\n- Use `{user_scope}` only for stable preferences or personal working context.\n- Use `{agent_scope}` only for private scratch context."
        tool_tips = """## Antigravity Tips

- Add the MCP config using the `serverUrl` field instead of `url`.
- Restart Antigravity after adding MCP servers if the config does not appear immediately.
- Set `AGENT_CORE_API_KEY` in your shell environment before starting an Antigravity session.
"""
    else:
        scope_guide = f"- Default memory scope: `{default_scope}`\n- User scope: `{user_scope}`\n- Private scope: `{agent_scope}`"
        tool_tips = f"""## Generic MCP Client Tips

- The MCP endpoint is `{base_url}/mcp`. Your client should send requests as JSON with `{{"tool": "...", "params": {{...}}}}`.
- Authenticate using `Authorization: Bearer $AGENT_CORE_API_KEY` header or your client's equivalent auth mechanism.
- Available tools include: `memory_search`, `memory_get`, `memory_write`, `memory_retract`, `credential_get`, `credential_list`, `activity_update`, `activity_list`, `get_briefing`, `briefing_list`, `connectors_list`, `connectors_bindings_list`, `connectors_bindings_test`, `connectors_actions_list`, and `connectors_run`.
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

1. Search memory in `{default_scope}` for relevant context before starting work. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
2. Search memory in `{user_scope}` for relevant user preferences.
3. Create or update an activity record when starting a meaningful task.
4. Store durable decisions and handoff notes in `{default_scope}`.
5. Use `credential_list` and `credential_get` to retrieve credential references — never ask for raw secrets.
6. Use connector tools to discover and run available server-side connector bindings before asking the user to wire an external service manually.

## Memory Write Rules

- Choose `decision` for durable choices and rationale.
- Choose `fact` for objective workspace state or implementation details.
- Choose `preference` for stable user or team preferences.
- Choose `scratchpad` only for temporary notes.
- Use `{workspace_scope_label}` for workspace memory when a workspace is selected, `{user_scope}` for stable user preferences, and `{agent_scope}` for private scratch context.
- Domain and topic are optional exact-match search filters. Add them only when they will help future retrieval.
- Confidence is caller-assigned and can be filtered by search; importance affects result ranking.

## Credentials And Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check Agent Core before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `AC_SECRET_*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_list` and `connectors_bindings_list` to discover server-side connector bindings available to this agent.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when Agent Core should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside Agent Core.

## API Key

Set `AGENT_CORE_API_KEY` in your local environment using the key shown when this agent was created or rotated.
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
        f"This setup is workspace-aware. Generated prompts use `{workspace_scope}` for workspace memory."
        if workspace_scope
        else f"No workspace is selected. Generated prompts use `{user_scope}` as the default shared context."
    )
    return f"""# Agent Core Instructions

Use these steps to connect an AI tool to Agent Core for {user_display}.

## What To Generate

1. Generate `MCP Config` when you are ready to connect the tool to Agent Core. This is the normal connection step for MCP-capable tools.
2. Generate `Environment Variables` only when your MCP config or launcher reads values from environment variables.
3. Generate `CLAUDE.md` or `AGENTS.md` for reusable repository-level guidance shared by multiple agents.
4. Generate `Session Prompt` when you want one-time agent-specific instructions pasted into a chat/session.
5. Generate `Verification Prompt` after setup and paste it into the connected agent to run the end-to-end verification flow.

Prompts can steer behavior and verify connectivity. They cannot install MCP servers, plugins, or skills by themselves.

## Connection Key

Click the one-time key button on `MCP Config` when you are ready to connect the tool.
Use the one-time key button on `Environment Variables` only when you need shell or launcher environment variables.
That rotates this agent's API key and inserts the new key into the generated output.
The key is shown once. Generating again invalidates the previous key for this connection.
The API key is the authoritative agent identity. Agent Core identifies requests as `{agent_scope}` by looking up the bearer token; repo instruction files do not set identity.

## Where Things Go

- MCP Config belongs in the MCP configuration location for the tool you are connecting. For Claude Code, run `claude mcp add` (CLI) or create `.mcp.json` in the repo root; for Codex CLI, that is `~/.codex/config.toml`; for OpenCode, add the OpenCode block under `mcp` in `~/.config/opencode/opencode.json`.
- Environment variables belong in your shell profile, launcher, service environment, or tool-specific environment settings. They do not connect Agent Core by themselves.
- Session Prompt is pasted into the first message or custom instructions for a single session. Use it to bootstrap behavior when no plugin or hook layer exists.
- `CLAUDE.md` and `AGENTS.md` belong in the workspace/repository root when you want persistent per-repository behavior. These files are workspace-centric and can be shared by Codex, OpenCode, Claude Code, and other agents using their own MCP keys.

## Selected Context

- Agent Core URL: `{base_url}`
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

Use Agent Core MCP for durable workspace memory, handoffs, and workspace context. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.
Default memory scope for this setup is `{default_scope}`.
Use your private scope `{agent_scope}` only for tool-specific scratch context.
Use full prefixed scope names exactly as shown; do not use plain workspace IDs or agent IDs as memory scopes.
Read `{user_scope}` for stable {user_display} preferences when relevant.
Use credential references through Agent Core MCP; never request or print raw secrets.
Activity records are operational task tracking, not durable memory. At the start of every non-trivial user task, immediately call `activity_update` with a concise `task_description`, `memory_scope` set to `{default_scope}`, and `status` set to `active`. While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Before your final response, call `activity_update` with `status: completed` when the task is complete, or `status: blocked` if you cannot proceed and need user input.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client has hooks or plugins, use them to automate memory/activity capture; if it does not, treat this prompt as the source of truth for those expectations.
When an external service is needed, use `credential_list`, `credential_get`, `connectors_list`, `connectors_bindings_list`, `connectors_actions_list`, `connectors_bindings_test`, and `connectors_run` instead of asking the user to hand-wire secrets or run manual service calls.

## Memory Workflow

At the start of a meaningful task:

1. Start or refresh the activity record using `activity_update`.
2. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
3. Search `{user_scope}` only when user preferences or personal workflow may matter.
4. Use `memory_get` for a known record id; otherwise prefer `memory_search`.

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in `{default_scope}` for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in `{user_scope}` for stable user preferences that apply beyond this one task.
- `scratchpad` in `{agent_scope}` for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes. Use concise content, add domain/topic when useful for exact filtering, set confidence to match certainty, and set importance higher only for information likely to matter later.
Use this prompt to bootstrap behavior in clients without lifecycle hooks; it is not a substitute for a configured MCP server or plugin.

Start by confirming you can reach Agent Core at {base_url}/mcp, then search `{default_scope}` for relevant context before making changes.
"""


def _build_claude_md(
    base_url, user_scope, workspace_scope, agent_scope, agent_display, workspace_name
):
    default_scope = (
        workspace_scope
        or "the authenticated/default user scope from your Agent Core connection"
    )
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    private_scope_guidance = "Use your authenticated Agent Core private scope, usually `agent:<your-agent-id>`, only for tool-specific scratch context."
    return f"""# Agent Core Workspace Context

You are working on the {workspace_name} workspace.

Use Agent Core for durable workspace memory, activity tracking, handoffs, and credential references. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.

## Connection

- **Agent Core URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active Agent Core user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not add API keys to this file.

## Memory Scopes

Use `{default_scope}` for default memory in this setup.
Read the authenticated/default user scope from your Agent Core connection only for stable personal preferences when relevant.
{private_scope_guidance}
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs like `{workspace_name}` or agent IDs like `{agent_display}` as memory scopes.

## Memory Workflow

At the start of a meaningful task:

1. Start or refresh the activity record using the Activity Tracking workflow below.
2. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
3. Search the authenticated/default user scope only when user preferences or personal workflow may matter.
4. Use `memory_get` for a known record id; otherwise prefer `memory_search`.

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in `{default_scope}` for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope for stable user preferences that apply beyond this one task.
- `scratchpad` in the authenticated private agent scope for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.

Keep memory content concise. Add domain/topic when useful for exact filtering. Set confidence to match certainty. Set importance higher only for information likely to matter later.

## Credentials

Use `credential_get` to retrieve `AC_SECRET_*` references. The Credential Broker resolves them at execution time.
Never ask users for raw credential values.

## Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check Agent Core before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `AC_SECRET_*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_list` and `connectors_bindings_list` to discover server-side connector bindings available to this agent.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when Agent Core should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside Agent Core.

## Activity Tracking

Activity records are operational task tracking, not durable memory.

At the start of every non-trivial user task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{default_scope}`
- `status`: `active`

When the task is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes. Use workspace memory, not agent-private scratch notes, as the durable source of truth for prior work.
Use `activity_list` and `briefing_list` when you need to inspect that trail from MCP instead of the dashboard.

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Update `task_description` if the task changes materially.

Before your final response, call `activity_update` with `status: completed` when the task is complete. Use `status: blocked` if you cannot proceed and need user input. Do not create activity records for trivial one-shot answers that do not inspect or modify project state.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client supports hooks or plugins, use them to automate these calls. If it does not, keep using this file as the manual operating contract.


## Claude Code Notes

- Claude Code automatically reads this `CLAUDE.md` file when present in the workspace root.
- Claude Code uses the configured MCP connection or your shell environment's `AGENT_CORE_API_KEY`. That key determines which Agent Core user and agent are active.
- Do not add your API key to this file.
- If Claude Code can't reach Agent Core, run the Verification Prompt output to verify the full end-to-end setup.
"""


def _build_agents_md(
    base_url, user_scope, workspace_scope, agent_scope, workspace_name
):
    default_scope = (
        workspace_scope
        or "the authenticated/default user scope from your Agent Core connection"
    )
    workspace_scope_label = workspace_scope or "No workspace scope selected"
    return f"""# Agent Core Workspace Context

You are working on the {workspace_name} workspace.

## Agent Core

Use Agent Core MCP for memory, credential references, and activity tracking. If this is a handoff, resume, or review of prior work, also inspect the recent activity trail and any generated briefing before making changes. Use `activity_list` and `briefing_list` when you need that trail from MCP.

- **Base URL:** {base_url}
- **Workspace scope:** {workspace_scope_label}

The active Agent Core user and agent identities are determined by the MCP/API key configured in your tool, not by this file. Do not commit credentials to this file.

## Memory Scope Guidance

Default memory scope for this setup is `{default_scope}`.
Read the authenticated/default user scope from your Agent Core connection for stable personal preferences when relevant.
Use your authenticated Agent Core private scope, usually `agent:<your-agent-id>`, for private scratch notes only.
Use full prefixed scope names exactly as shown. Do not use plain workspace IDs or agent IDs as memory scopes.

## Activity Workflow

Activity records are operational task tracking, not durable memory.

At the start of every non-trivial user task, call `activity_update` immediately with:

- `task_description`: a concise description of the current task
- `memory_scope`: `{default_scope}`
- `status`: `active`

When the task is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes. Use workspace memory, not agent-private scratch notes, as the durable source of truth for prior work.
Use `activity_list` and `briefing_list` when you need to inspect that trail from MCP instead of the dashboard.

While actively working, call `activity_update` again every 1-2 minutes as a heartbeat. Update `task_description` if the task changes materially.

Before your final response, call `activity_update` with `status: completed` when the task is complete. Use `status: blocked` if you cannot proceed and need user input. Do not create activity records for trivial one-shot answers that do not inspect or modify project state.
If the session has to stop early or hits a token limit, leave the activity current and write durable decisions or handoff notes to memory so another agent can continue from the saved state.
If work needs to move across users or workspaces, make that explicit in the activity scope and handoff notes rather than assuming a hidden policy layer.
If the client supports hooks or plugins, use them to automate these calls. If it does not, keep using this file as the manual operating contract.

## Memory Workflow

At the start of a meaningful task:

1. Confirm Agent Core is reachable at {base_url}/mcp if this is a new setup or connectivity is uncertain.
2. Start or refresh the activity record using the Activity Workflow above.
3. Search `{default_scope}` with 2-3 focused queries for relevant architecture, decisions, prior bugs, and current project state. If the search returns little or nothing, retry with exact topic values, exact keywords from prior records, or a known record id. When embeddings are unavailable, broad conceptual queries can miss; exact tokens and known ids are more reliable.
4. If this is a handoff, resume, or review of prior work, inspect the recent activity trail and any generated briefing before making changes.
5. Search the authenticated/default user scope only when user preferences or personal workflow may matter.
6. Use `memory_get` for a known record id; otherwise prefer `memory_search`.

Write memory only when it will help a future session:

- `decision` in `{default_scope}` for durable choices, tradeoffs, rejected options, and why they were chosen.
- `fact` in `{default_scope}` for stable implementation facts, integration details, constraints, and verified behavior.
- `preference` in the authenticated/default user scope for stable user preferences that apply beyond this one task.
- `scratchpad` in the authenticated private agent scope for temporary private notes, or in `{default_scope}` only for short-lived workspace handoff notes.

Do not write memory for routine progress, command output, facts already obvious from files, secrets, raw credentials, or noisy transient debugging notes.

Keep memory content concise. Add domain/topic when useful for exact filtering. Set confidence to match certainty. Set importance higher only for information likely to matter later.

## Credentials And Connectors

When a task may require an external service, credential, API token, repository host, chat service, browser service, or Composio-style connector, check Agent Core before asking the user for setup details.

1. Use `credential_list` to discover available credential references in authorized scopes.
2. Use `credential_get` only when you need a specific `AC_SECRET_*` reference for a local tool or command. Never ask the user for raw secrets and never print raw secrets.
3. Use `connectors_list` and `connectors_bindings_list` to discover server-side connector bindings available to this agent.
4. Use `connectors_actions_list` before running an unfamiliar connector action.
5. Use `connectors_bindings_test` when a binding may be stale or unverified.
6. Use `connectors_run` when Agent Core should perform the external action server-side.

Prefer connector bindings over local secret handling when both are available, because the raw credential stays inside Agent Core.
This file is the manual fallback when the client has no lifecycle hook or plugin layer.

## Codex Notes

- Codex reads `AGENTS.md` at the start of each session.
- This file is workspace-centric and can be shared by multiple agents in the same repository. The MCP/API key determines whether the active agent is Codex, OpenCode, Claude Code, or another configured agent.
- For multi-agent collaboration, select a workspace and ensure each agent has read/write access to that workspace scope.
- Use the MCP tools (`memory_search`, `memory_write`, `activity_update`, `credential_list`, `credential_get`, `connectors_*`) rather than raw API calls for better scope enforcement.
- If Codex loses connectivity, run the verification prompt to verify the full end-to-end setup.
"""


def _connection_key_value(api_key=None):
    return api_key or "{{AGENT_CORE_API_KEY}}"


def _build_mcp_json(base_url, api_key=None):
    key = _connection_key_value(api_key)
    codex_auth = f'http_headers = {{ Authorization = "Bearer {key}" }}'
    generic_json = json.dumps(
        {
            "mcpServers": {
                "agent-core": {
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
                "agent-core": {
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
                "agent-core": {
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
claude mcp add --transport http --scope user agent-core {base_url}/mcp \\
  --header "Authorization: Bearer {key}"

# Option 2 — create .mcp.json in your repo root (workspace-level, committable):
# If committing, replace the key with an env var: "Bearer ${{AGENT_CORE_API_KEY}}"
{generic_json}

# Codex CLI: add this to ~/.codex/config.toml
[mcp_servers.agent-core]
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
    return f"""# Agent Core Environment Variables
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
        return "Use the section that matches your tool. For Claude Code, run the <code>claude mcp add</code> command or save the JSON as <code>.mcp.json</code> in your repo root. Antigravity uses the same MCP shape but expects <code>serverUrl</code> instead of <code>url</code>. The bearer key determines the active Agent Core agent."
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
    return f"""Run the Agent Core verification flow end to end:

1. Call `activity_update` with `task_description` set to "Agent Core verification", `memory_scope` set to `{workspace_scope}`, and `status` set to `active`.
2. Write a memory record to `{workspace_scope}` that says this agent is connected, includes the current verification context, and is safe to use for this workspace. Capture the returned record id.
3. Call `memory_get` for that record id and confirm the record is readable from the workspace scope.
4. Call `memory_search` in `{workspace_scope}` for an exact token from the record you just wrote and report the result. If the first search returns zero results, retry once with the exact token plus the `domain` and `topic` from the record.
5. Call `credential_list` and report whether credential references are visible. Do not reveal or print raw secrets.
6. Call `connectors_list` to list connector types. Then call `connectors_bindings_list` with no scope to see everything visible to this agent. If you can read `{user_scope}`, call `connectors_bindings_list` again with `scope` set to `{user_scope}`. If you can read `{workspace_scope}`, call `connectors_bindings_list` again with `scope` set to `{workspace_scope}`. Report user-scoped and workspace-scoped bindings separately if both exist.
7. Call `connectors_actions_list` with a real connector type id from the `connectors_list` result and pass it as `connector_type_id` exactly. Report whether connector actions are visible.
8. If at least one enabled binding is visible in any scope, call `connectors_bindings_test` on a non-destructive binding and report the result. If none are visible, say that clearly.
9. Call `activity_update` with `status` set to `completed`.
10. Report which scope you wrote to and summarize the memory, credential, and connector checks.

Use the full prefixed scope name exactly as shown. Do not use a plain workspace ID as a memory scope.
"""
