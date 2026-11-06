"""Overview dashboard page and global search."""

import asyncio
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
        memory_records, _ = await asyncio.to_thread(
            memory_service.search_memory,
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
    for activity in activity_rows:
        metadata = _json_loads(activity.get("metadata_json"))
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
        activity_hits.append(
            _search_result(
                "activity",
                activity.get("task_description", "")[:120],
                summary,
                "/activity",
                "Activity",
            )
        )
        if len(activity_hits) >= limit:
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
                "connector_types": len(connector_type_hits),
                "bindings": len(binding_hits),
            },
            "memory": memory_hits,
            "activities": activity_hits,
            "connector_types": connector_type_hits,
            "bindings": binding_hits,
        }
    )


def _waiting_rows(session: dict, is_admin: bool, attention: list[dict]) -> list[tuple[str, str, str]]:
    """What is waiting on this person: (what it is, the text, where to deal with it)."""
    from app.services import (
        activity_service,
        approval_service,
        credential_request_service,
        memory_proposal_service,
    )

    rows: list[tuple[str, str, str]] = []
    if is_admin:
        for a in approval_service.list_for_scopes(None, "pending", 5):
            rows.append(("Approval", f"{a['title']} (proposed by {a['proposed_by']})", f"/approvals/{a['id']}"))
    for h in activity_service.list_needs_human(user_id=None if is_admin else session["user_id"], limit=5):
        rows.append(("Handoff", f"{h['agent_id']} asks: {(h.get('needs_human') or '')[:140]}", "/activity"))
    for a in attention[:5]:
        rows.append((str(a.get("status", "")).capitalize(), (a.get("task_description") or "")[:140], "/activity"))
    if is_admin:
        for r in credential_request_service.list_pending()[:5]:
            rows.append(("Credential", f"{r.get('requested_by_agent_id') or 'An agent'} needs {r['name']} in {r['scope']}", "/credentials"))
        suggestions = len(memory_proposal_service.list_proposals(status="pending", limit=200))
        if suggestions:
            rows.append(("Memory", f"{suggestions} suggestion{'s' if suggestions != 1 else ''} to review", "/memory"))
    return rows


def _health_items(is_admin: bool) -> list[tuple[str, str, str, bool]]:
    """(label, value, where to look, is it fine) for the admin's health strip."""
    from datetime import datetime, timedelta, timezone

    from app.branding import APP_VERSION
    from app.database import get_db
    from app.services import broker_service, network_policy_service

    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with get_db() as conn:
        failing = conn.execute(
            "SELECT COUNT(*) FROM connector_bindings WHERE enabled = 1 AND COALESCE(last_error, '') != ''"
        ).fetchone()[0]
        dead = conn.execute(
            "SELECT COUNT(*) FROM webhook_delivery_log WHERE status = 'dead' AND COALESCE(last_attempt_at, created_at) >= ?",
            (cutoff,),
        ).fetchone()[0]
    broker = broker_service.get_broker_credential_hash() is not None
    return [
        ("Agent Core", f"version {APP_VERSION}, running", "", True),
        ("Services", "all working" if not failing else f"{failing} failing", "/connectors", not failing),
        ("Webhooks", "no failures in 24 hours" if not dead else f"{dead} failed in 24 hours", "/webhooks", not dead),
        ("Local credential helper", "ready" if broker else "not set up", "/settings", broker),
        *[(label, value, "/settings#security", fine) for label, value, fine in network_policy_service.health_lines()],
    ]


@router.get("/")
def dashboard_home(request: Request, session: dict = Depends(require_auth)):
    from app.services import activity_service
    from app.services.agent_service import list_agents

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"

    if is_admin:
        agents = list_agents()
        agent_count = len([a for a in agents if a.get("is_active")])
        recent_activity = activity_service.list_activities(limit=8)
        attention = activity_service.list_attention_activities(limit=8)
    else:
        agents = list_agents(owner_user_id=session["user_id"])
        agent_count = len([a for a in agents if a.get("is_active")])
        recent_activity = activity_service.list_activities(
            user_id=session["user_id"], limit=8
        )
        attention = activity_service.list_attention_activities(
            user_id=session["user_id"], limit=8
        )

    waiting = _waiting_rows(session, is_admin, attention)
    if waiting:
        waiting_body = "<table><tbody>" + "".join(
            f"<tr><td><span class='badge'>{escape_html(kind)}</span></td><td>{escape_html(text)}</td>"
            f"<td><a href='{escape_html(href)}'>Open</a></td></tr>"
            for kind, text, href in waiting
        ) + "</tbody></table>"
    else:
        waiting_body = '<p class="text-muted" style="margin:0">Nothing is waiting on you.</p>'
    attention_html = f"""
    <div class="card attention-card" id="overview-waiting">
      <h2>Waiting on you</h2>
      <p class="text-muted" style="margin-top:-6px">Decisions and requests that need a person. Each row links to where you deal with it.</p>
      {waiting_body}
    </div>"""

    health_html = ""
    if is_admin:
        health_html = (
            '<div class="card" id="overview-health"><h2>Health</h2><div class="stat-grid">'
            + "".join(
                (f"<a class='stat-card stat-link' href='{escape_html(href)}'>" if href else "<div class='stat-card'>")
                + f"<div class='label'>{escape_html(label)}</div>"
                + f"<div class='{'' if ok else 'text-danger'}'>{escape_html(value)}</div>"
                + ("</a>" if href else "</div>")
                for label, value, href, ok in _health_items(is_admin)
            )
            + "</div></div>"
        )

    started_html = ""
    if is_admin and (agent_count == 0 or not recent_activity):
        steps = [
            ("Add an agent", "/agents", agent_count > 0, "An agent is one AI tool's identity in Agent Core."),
            ("Connect its tool", "/integrations", bool(recent_activity), "Copy the key and settings into the tool."),
            ("Give it a service", "/connectors", False, "Optional: let it act in an outside system, such as GitHub."),
            ("Watch it work", "/activity", bool(recent_activity), "Its tasks and handoffs appear here."),
        ]
        step_items = []
        for label, href, done, note in steps:
            style = " style='opacity:.6'" if done else ""
            step_items.append(f"<li{style}><a href='{href}'>{label}</a> <span class='text-muted'>{note}</span></li>")
        started_html = (
            '<div class="card" id="overview-getting-started"><h2>Getting started</h2><ol>'
            + "".join(step_items) + "</ol></div>"
        )

    search_html = """
    <div class="card">
      <div class="section-header">
        <h2>Search</h2>
        <div class="section-note">Find a memory, a task or a service.</div>
      </div>
      <form onsubmit="runDashboardSearch(event)" style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
        <input type="text" id="dashboard-search-query" class="search-input" placeholder="Search memories, tasks and services..." style="min-width:260px;flex:1" autocomplete="off">
        <button type="submit" class="btn">Search</button>
        <button type="button" class="btn btn-secondary" onclick="clearDashboardSearch()">Clear</button>
      </form>
      <div id="dashboard-search-results" style="margin-top:14px">
        <div class="empty">Type a word to search memories, tasks and services.</div>
      </div>
    </div>"""

    activity_rows = (
        "".join(
            f"<tr><td><div class='text-truncate' title='{escape_html(a.get('task_description', ''))}'>{escape_html(a.get('task_description', ''))}</div></td>"
            f"<td data-label='Status'><span class='badge badge-{escape_html(a.get('status', 'active'))}'>{escape_html(a.get('status', ''))}</span></td>"
            f"<td data-label='Agent'>{escape_html(a.get('assigned_agent_id', ''))}</td>"
            f"<td class='hide-phone'>{local_dt(a.get('updated_at') or a.get('started_at'))}</td></tr>"
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
        {APP_NAME} gives your AI agents shared memory, a record of their work, and safe access to your services and credentials. Start with what is waiting on you.
      </p>
    </div>
    {started_html}
    {attention_html}
    {health_html}
    {search_html}
    <div class="card">
      <div class="section-header">
        <h2>Recent Activity</h2>
        <div class="section-actions">
          <a href="/activity" class="btn btn-sm btn-secondary">View Activity</a>
          {audit_link}
        </div>
      </div>
      <table class="stack-phone"><thead><tr><th>Task</th><th>Status</th><th>Agent</th><th>Updated</th></tr></thead><tbody id="overview-activity-tbody">{activity_rows}</tbody></table>
    </div>
    <script>
    window.{JS_WINDOW_EVENT} = async function(event) {{
      if (!(event.type || '').startsWith('activity_')) return;
      var j = await apiFetch('/api/dashboard/activity/summary');
      if (!j.ok) return;
      var data = j.data || {{}};
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
          '<td data-label="Status"><span class="badge badge-' + escapeHtml(a.status || '') + '">' + escapeHtml(a.status || '') + '</span></td>' +
          '<td data-label="Agent">' + escapeHtml(a.assigned_agent_id || '') + '</td>' +
          '<td class="hide-phone">' + ts + '</td>' +
        '</tr>';
      }}).join('');
      applyLocalTimes(tbody);
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
            '<h3 style="margin:0" class="sub-heading">' + escapeHtml(title) + '</h3>' +
            '<div class="section-note">' + items.length + ' match(es)</div>' +
          '</div>' +
          items.map(renderItem).join('') +
        '</div>';
      }}
      const sections = [
        renderSection('Memory', data.memory || []),
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
      results.innerHTML = '<div class="empty">Type a word to search memories, tasks and services.</div>';
      input.focus();
    }}
    </script>
    """,
        "/",
        session=session,
    )
