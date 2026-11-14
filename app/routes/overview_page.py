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
    for connection in visible_bindings:
        if not _query_in_values(
            query,
            connection.get("name", ""),
            connection.get("scope", ""),
            connection.get("connector_display_name", ""),
            connection.get("connector_type_id", ""),
            connection.get("last_error", ""),
            connection.get("config_json", ""),
        ):
            continue
        binding_hits.append(
            _search_result(
                "binding",
                connection.get("name", "")[:120],
                " · ".join(
                    part
                    for part in [
                        connection.get("connector_display_name", ""),
                        connection.get("scope", ""),
                        "enabled" if connection.get("enabled") else "disabled",
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


def _ago(value) -> str:
    """How long ago, in words a person reads: "just now", "3 hours ago", "2 days ago"."""
    from app.time_utils import parse_utc_datetime, utc_now

    try:
        seconds = (utc_now() - parse_utc_datetime(value)).total_seconds()
    except Exception:
        return ""
    if seconds < 90:
        return "just now"
    for size, unit in ((86400, "day"), (3600, "hour"), (60, "minute")):
        if seconds >= size:
            n = int(seconds // size)
            return f"{n} {unit}{'' if n == 1 else 's'} ago"
    return ""


GETTING_STARTED_HIDDEN_KEY = "getting_started_hidden:{user_id}"


def getting_started_hidden(user_id: str) -> bool:
    from app.services import system_settings_service

    return system_settings_service.read_string(GETTING_STARTED_HIDDEN_KEY.format(user_id=user_id), "") == "1"


def _getting_started_html(session, is_admin, agent_count, recent_activity, otp_item) -> tuple[str, str]:
    """The getting-started list as (open card, quiet line).

    While a required step (workspace, agent, connect its tool, watch it work) is undone it is an open card, below
    anything waiting on a person. Once those are done it shrinks to one quiet line at the bottom that opens the list
    (a person can hide it for good; Settings > Account brings it back). When every step is done it is gone."""
    if not is_admin:
        return "", ""
    from app.services import connector_service, workspace_service

    has_workspace = bool(workspace_service.list_workspaces())
    has_activity = bool(recent_activity)
    has_service = bool(connector_service.list_bindings(enabled=True))  # at least one enabled connection exists
    required = [
        ("Create a workspace", "/workspaces", has_workspace, "The shared place where your agents keep memory and tasks."),
        ("Add an agent", "/agents", agent_count > 0, "An agent is one AI tool's identity; it is set up to work in your workspace."),
        ("Connect its tool", "/integrations", has_activity, "Copy the key and settings into the tool."),
        ("Watch it work", "/activity", has_activity, "Its tasks and handoffs appear here."),
    ]
    optional = [
        ("Give it a service", "/connectors", has_service, "Optional: let it act in an outside system, such as GitHub."),
    ]
    two_step_done = not otp_item
    optional_left = len([1 for _, _, done, _ in optional if not done]) + (0 if two_step_done else 1)
    required_left = [1 for _, _, done, _ in required if not done]

    def items(rows):
        out = []
        for label, href, done, note in rows:
            style = " style='opacity:.6'" if done else ""
            out.append(f"<li{style}><a href='{href}'>{label}</a> <span class='text-muted'>{note}</span></li>")
        return "".join(out)

    if required_left:
        return (
            '<div class="card" id="overview-getting-started"><h2>Getting started</h2><ol>'
            + items(required) + items(optional) + otp_item + "</ol></div>",
            "",
        )
    if not optional_left or getting_started_hidden(session["user_id"]):
        return "", ""
    label = f"Getting started: set up \u00b7 {optional_left} optional step{'' if optional_left == 1 else 's'} left"
    return (
        "",
        '<details class="getting-started-fold" id="overview-getting-started"><summary>' + label + "</summary>"
        "<ol>" + items(required) + items(optional) + otp_item + "</ol>"
        "<p><button type='button' class='btn btn-sm btn-secondary' data-hide-getting-started>Hide this</button> "
        "<span class='form-hint'>You can bring it back in Settings, under Account.</span></p></details>",
    )


def _waiting_rows(session: dict, is_admin: bool) -> list[dict]:
    """What is waiting on this person, each as a dict the page renders: what it is about, where, from whom, how long.

    Read from the one source Overview, Activity and `attention_list` share. An admin sees every group;
    anyone else sees the handoffs addressed to them. A task that stopped is not listed: it closes itself.
    """
    from app.services import waiting_service, workspace_service
    from app.services.agent_service import list_agents

    groups = ("approvals", "handoffs", "credential_requests") if is_admin else ("handoffs",)
    waiting = waiting_service.waiting_on_person(
        scopes=None, user_id=None if is_admin else session["user_id"], include=groups, limit=5
    )
    agent_names = {a["id"]: (a.get("display_name") or a["id"]) for a in list_agents()}
    workspace_names = {f"workspace:{w['id']}": (w.get("name") or w["id"]) for w in workspace_service.list_workspaces()}
    rows: list[dict] = []
    for a in waiting["approvals"]:
        rows.append({"kind": "Approval", "title": a["title"], "sub": f"Approval proposed by {a['proposed_by']}", "text": "", "href": f"/approvals/{a['id']}"})
    for h in waiting["handoffs"]:
        agent = agent_names.get(h["agent_id"], h["agent_id"])
        scope = h.get("memory_scope") or ""
        workspace = workspace_names.get(scope) or (
            f"{agent}'s own memory" if scope.startswith("agent:")
            else "your own memory" if scope.startswith("user:")
            else "its own memory" if not scope else scope.replace("workspace:", "")
        )
        age = _ago(h.get("asked_at") or h.get("updated_at"))
        rows.append(
            {
                "kind": "Question",
                "title": (h.get("task_description") or "A task")[:160],
                "sub": " · ".join(p for p in (workspace, agent, f"asked {age}" if age else "") if p),
                "text": (h.get("needs_human") or "")[:400],
                "href": f"/activity?task={h['id']}",
                "activity_id": h["id"],
                "agent": agent,
                "workspace": workspace,
                "url": h.get("needs_human_url"),
            }
        )
    for r in waiting["credential_requests"]:
        rows.append(
            {
                "kind": "Credential",
                "title": f"{r.get('requested_by') or 'An agent'} needs {r['name']}",
                "sub": f"Credential request · {workspace_names.get(r['scope'], r['scope'])}",
                "text": r.get("purpose") or "",
                "href": "/credentials",
                "request_id": r["request_id"],
            }
        )
    return rows


def _health_items(is_admin: bool) -> list[tuple[str, str, str, bool]]:
    """(label, short state, where to look, is it fine) for the live tiles of the admin's health strip."""
    from datetime import datetime, timedelta, timezone

    from app.branding import APP_VERSION
    from app.database import get_db
    from app.services import broker_service

    cutoff = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    with get_db() as conn:
        failing = conn.execute(
            "SELECT COUNT(*) FROM connector_bindings WHERE enabled = 1 AND COALESCE(last_error, '') != ''"
        ).fetchone()[0]
        total_bindings = conn.execute("SELECT COUNT(*) FROM connector_bindings WHERE enabled = 1").fetchone()[0]
        checked = conn.execute(
            "SELECT COUNT(*) FROM connector_bindings WHERE enabled = 1 AND last_tested_at IS NOT NULL"
        ).fetchone()[0]
        dead = conn.execute(
            "SELECT COUNT(*) FROM webhook_delivery_log WHERE status = 'dead' AND COALESCE(last_attempt_at, created_at) >= ?",
            (cutoff,),
        ).fetchone()[0]
    broker = broker_service.get_broker_credential_hash() is not None
    # "All working" is a claim about checks: every enabled connection's last check passed (or could not be run), and
    # at least one has been checked. A connection whose check failed is "failing".
    if not total_bindings:
        services_value = "none set up"
    elif failing:
        services_value = f"{failing} failing"
    elif not checked:
        services_value = "not checked yet"
    elif checked < total_bindings:
        services_value = f"{checked} of {total_bindings} checked, none failing"
    else:
        services_value = "all working"
    return [
        ("Agent Core", f"version {APP_VERSION}, running", "", True),
        ("Services", services_value, "/connectors", not failing),
        ("Webhooks", "no failures in 24 hours" if not dead else f"{dead} failed in 24 hours", "/webhooks", not dead),
        ("Local credential helper", "ready" if broker else "not set up", "/settings#security", broker),
    ]


def _limit_items() -> list[tuple[str, str, str, bool]]:
    """The two limits the owner has chosen, shown quieter than the live tiles."""
    from app.services import network_policy_service

    return network_policy_service.health_lines()


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
    else:
        agents = list_agents(owner_user_id=session["user_id"])
        agent_count = len([a for a in agents if a.get("is_active")])
        recent_activity = activity_service.list_activities(
            user_id=session["user_id"], limit=8
        )

    waiting = _waiting_rows(session, is_admin)
    if waiting:
        from urllib.parse import urlparse

        def action_cell(row):
            if row.get("request_id"):  # a credential request is answered right here: type the value, Save
                rid = escape_html(row["request_id"])
                return (
                    f"<form class='inline-answer' data-credential-request='{rid}'>"
                    "<input type='password' autocomplete='new-password' required placeholder='Value' aria-label='Credential value'> "
                    "<button class='btn btn-sm' type='submit'>Save</button></form>"
                )
            if row["kind"] == "Approval":
                return f"<a class='btn btn-sm' href='{escape_html(row['href'])}'>Review</a>"
            aid = escape_html(row["activity_id"])
            agent, workspace = escape_html(row["agent"]), escape_html(row["workspace"])
            url = row.get("url")
            form = (
                f"<form class='inline-answer' data-handoff-answer='{aid}'>"
                "<input type='text' maxlength='1000' placeholder='Your answer (optional)' aria-label='Your answer'> "
                "<button class='btn btn-sm btn-secondary' type='submit' data-empty-label='Mark as dealt with' data-typed-label='Send answer'>Mark as dealt with</button></form>"
            )
            if url:
                host = escape_html(urlparse(url).hostname or url)
                return (
                    f"<a class='btn' href='{escape_html(url)}' target='_blank' rel='noopener noreferrer'>Open in {host}</a>"
                    f"<div class='form-hint' style='margin:8px 0 4px'>Or tell {agent} here:</div>{form}"
                    f"<p class='form-hint' style='margin:4px 0 0'>This tells {agent}; it does not carry it out over there. "
                    f"{agent} sees it the next time it works in {workspace}, and this closes.</p>"
                )
            return (
                form
                + f"<p class='form-hint' style='margin:4px 0 0'>Your answer goes to {agent} the next time it works in {workspace}, and this closes.</p>"
            )

        waiting_body = "<table><tbody>" + "".join(
            "<tr><td class='waiting-what'>"
            f"<strong>{escape_html(row['title'])}</strong>"
            f"<div class='text-muted' style='font-size:0.85rem'>{escape_html(row['sub'])}</div>"
            + (f"<div style='margin-top:4px'>{escape_html(row['text'])}</div>" if row.get("text") else "")
            + (
                f"<div style='margin-top:4px'><a href='{escape_html(row['href'])}'>See the task and its notes</a></div>"
                if row["kind"] == "Question"
                else ""
            )
            + f"</td><td class='waiting-act'>{action_cell(row)}</td></tr>"
            for row in waiting
        ) + "</tbody></table>"
    else:
        waiting_body = (
            '<p class="text-muted" style="margin:0">Nothing needs you right now. Nothing is waiting on a person.</p>'
            if agent_count
            else '<p class="text-muted" style="margin:0">Nothing needs you yet. <a href="/agents">Add an agent</a> to get started.</p>'
        )
    attention_html = f"""
    <div class="card attention-card" id="overview-waiting">
      <h2>Waiting on you</h2>
      <p class="text-muted" style="margin-top:-6px">Decisions and requests that need a person. An empty list means there is nothing to do.</p>
      {waiting_body}
    </div>"""

    health_html = ""
    if is_admin:
        def tile(label, value, href, ok, quiet=False):
            classes = "stat-card stat-tile" + (" stat-limit" if quiet else "")
            open_tag = f"<a class='{classes} stat-link' href='{escape_html(href)}'>" if href else f"<div class='{classes}'>"
            return (
                open_tag
                + f"<div class='label'>{escape_html(label)}</div>"
                + f"<div class='tile-value{'' if ok else ' text-danger'}'>{escape_html(value)}</div>"
                + ("</a>" if href else "</div>")
            )

        health_html = (
            '<div class="card" id="overview-health"><h2>Health</h2><div class="stat-grid">'
            + "".join(tile(*item) for item in _health_items(is_admin))
            + "</div>"
            + '<div class="stat-group-label">Limits you set</div><div class="stat-grid stat-grid-limits">'
            + "".join(tile(*item, quiet=True) for item in _limit_items())
            + "</div></div>"
        )

    from app.services.auth_service import is_otp_enrolled

    otp_item = "" if (not is_admin or is_otp_enrolled(session["user_id"])) else (
        "<li><a href='/settings/otp'>Turn on two-step sign-in</a> "
        "<span class='text-muted'>Optional, recommended once you are set up: a code from your phone at sign-in.</span></li>"
    )
    started_html, started_quiet_html = _getting_started_html(session, is_admin, agent_count, recent_activity, otp_item)

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
        "Needs you",
        f"""
    <div class="page-header">
      <h1>Needs you</h1>
      <p class="text-muted" style="max-width:760px;margin-top:8px">
        {APP_NAME} gives your AI agents shared memory, a record of their work, and safe access to your services and credentials. Start with what is waiting on you.
      </p>
    </div>
    {attention_html}
    {started_html}
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
    {started_quiet_html}
    <script>
    document.addEventListener('click', async function(event) {{
      var hide = event.target.closest && event.target.closest('[data-hide-getting-started]');
      if (!hide) return;
      var r = await apiFetch('/api/dashboard/user-settings', {{ method: 'POST', body: JSON.stringify({{ getting_started_hidden: true }}) }});
      if (r.ok) {{ var el = document.getElementById('overview-getting-started'); if (el) el.remove(); showToast('Hidden. Settings > Account brings it back.', 'success'); }}
    }});
    document.addEventListener('input', function(event) {{
      var box = event.target.closest && event.target.closest('form[data-handoff-answer] input');
      if (!box) return;
      var button = box.form.querySelector('button');
      button.textContent = box.value.trim() ? button.dataset.typedLabel : button.dataset.emptyLabel;
    }});
    document.addEventListener('submit', async function(event) {{
      var ask = event.target.closest && event.target.closest('form[data-handoff-answer]');
      if (ask) {{
        event.preventDefault();
        var answer = ask.querySelector('input').value.trim();
        var r = await apiFetch('/api/activity/' + encodeURIComponent(ask.dataset.handoffAnswer) + '/handoff/resolve', {{
          method: 'POST', body: JSON.stringify(answer ? {{ answer: answer }} : {{}})
        }});
        if (r.ok) {{ showToast('Sent. The agent sees your answer at its next session.', 'success'); location.reload(); }}
        else {{ showToast((r.error && r.error.message) || 'Could not send it', 'danger'); }}
        return;
      }}
      var form = event.target.closest && event.target.closest('form[data-credential-request]');
      if (!form) return;
      event.preventDefault();
      var requestId = form.dataset.credentialRequest;
      var input = form.querySelector('input');
      var j = await apiFetch('/api/credentials/requests/' + encodeURIComponent(requestId) + '/fulfill', {{
        method: 'POST', body: JSON.stringify({{ value: input.value }})
      }});
      input.value = '';
      if (j.ok) {{ showToast('Saved. The agent can use it now.', 'success'); location.reload(); }}
      else {{ showToast((j.error && j.error.message) || 'Could not save it', 'danger'); }}
    }});
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
