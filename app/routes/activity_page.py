"""Activity dashboard page."""

from urllib.parse import quote

from fastapi import APIRouter, Request, Depends

from app.branding import JS_WINDOW_EVENT
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    local_dt,
    _build_pagination,
    _page_number,
)

router = APIRouter()


def _ask_note_text(activity: dict) -> str:
    """How the task's question to a person ended, in words ("question answered 2026-11-13"), or "" when it is still open."""
    from app.services import activity_service

    handoff = activity_service.load_handoff(activity.get("handoff_json"))
    resolution = handoff.get("resolution") if isinstance(handoff, dict) else None
    if not handoff.get("needs_human") or not isinstance(resolution, dict):
        return ""
    kind = resolution.get("kind")
    when = str(resolution.get("at") or "")[:10]
    text = {
        "expired": "question lapsed: nobody answered",
        "person": "question answered",
        "agent": "question settled by the agent",
        "superseded": "question replaced by a newer one",
    }.get(kind, "question closed")
    return text + (" " + when if when else "")


def _ask_note(activity: dict) -> str:
    """One quiet line under the status when the task's question to a person is no longer waiting, and how it ended."""
    text = _ask_note_text(activity)
    return f"<br><span class='text-muted' style='font-size:0.75rem'>{escape_html(text)}</span>" if text else ""


def _task_cell(a: dict) -> str:
    """The task: its title on one or two lines. The full record opens in the shared detail dialog."""
    title = escape_html((a.get("task_description") or "")[:160])
    return f"<a href='#' class='activity-title' data-activity-detail='{escape_html(a['id'])}'>{title}</a>"


def _scope_words(scope: str, workspace_names: dict, agent_labels: dict) -> str:
    """Where a task lives, in plain words: a workspace's name, "private to <agent>", or personal."""
    scope = scope or ""
    if scope.startswith("workspace:"):
        return workspace_names.get(scope[len("workspace:"):], scope[len("workspace:"):])
    if scope.startswith("agent:"):
        agent = scope[len("agent:"):]
        return "private to " + agent_labels.get(agent, agent)
    if scope.startswith("user:"):
        return "personal"
    return scope or "—"


def _detail_record(a: dict, workspace_names: dict, agent_labels: dict) -> dict:
    """Everything the detail dialog shows for one task; empty parts are simply absent."""
    import json as _json

    from app.services import activity_service

    try:
        handoff = _json.loads(a.get("handoff_json") or "{}") or {}
    except (TypeError, ValueError):
        handoff = {}
    status = a.get("status", "")
    needs = handoff.get("needs_human") or ""
    return {
        "id": a["id"],
        "title": a.get("task_description") or "",
        "status": status,
        "agent": agent_labels.get(a.get("assigned_agent_id", ""), a.get("assigned_agent_id", "")),
        "workspace": _scope_words(a.get("memory_scope") or "", workspace_names, agent_labels),
        "started_at": a.get("started_at"),
        "updated_at": a.get("updated_at"),
        "task_ref": activity_service.task_ref(a),
        "result": a.get("task_result") or "",
        "note": a.get("task_note") if a.get("task_note") and a.get("task_note") != a.get("task_result") else "",
        "next_steps": [str(i) for i in (handoff.get("next_steps") or [])],
        "open_questions": [str(i) for i in (handoff.get("open_questions") or [])],
        # An ask that is no longer waiting shows only how it ended, not its text again.
        "needs_human": needs if (needs and not a.get("handoff_resolved_at")) else "",
        "needs_url": handoff.get("needs_human_url") or "",
        "waiting": bool(needs) and not a.get("handoff_resolved_at"),
        "ask_note": _ask_note_text(a) if needs and a.get("handoff_resolved_at") else "",
        "can_done": status in ("active", "stale", "blocked", "reassigned"),
        "can_start": status in ("stale", "blocked", "reassigned"),
        "can_cancel": status in ("active", "stale", "blocked", "reassigned"),
    }


@router.get("/activity")
def activity_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import activity_service
    from app.services.agent_service import list_agents
    from app.services import workspace_service, workspace_sync_service

    activity_service.mark_stale_activities()
    is_admin = session.get("role") == "admin"
    executions = workspace_sync_service.list_executions(
        limit=25, user_id=None if is_admin else session["user_id"]
    )
    execution_rows = "".join(
        f"<tr><td>{escape_html(e['agent_id'])}</td><td><code>{escape_html(e['memory_scope'])}</code></td>"
        f"<td><span class='badge badge-{escape_html(e['status'])}'>{escape_html(e['status'])}</span></td>"
        f"<td>{local_dt(e.get('last_seen_at'))}</td><td>{e.get('acknowledged_sequence') or 0}</td>"
        f"<td>{e.get('unacknowledged') or 0}</td></tr>"
        for e in executions
    )

    page = _page_number(request.query_params.get("page"))
    status_filter = request.query_params.get("status", "")
    limit = 50
    scope_user_id = None if is_admin else session["user_id"]

    total_activities = activity_service.count_activities(
        user_id=scope_user_id,
        status=status_filter or None,
    )
    total_pages = max(1, (total_activities + limit - 1) // limit)
    page = min(page, total_pages)
    offset = (page - 1) * limit

    activities = (
        activity_service.list_activities(
            user_id=scope_user_id,
            status=status_filter or None,
            limit=limit,
            offset=offset,
        )
        or []
    )
    all_agents = (
        list_agents() if is_admin else list_agents(owner_user_id=session["user_id"])
    )
    agent_labels = {a["id"]: a.get("display_name") or a["id"] for a in all_agents}
    workspace_names = {w["id"]: (w.get("name") or w["id"]) for w in (workspace_service.list_workspaces() or [])}
    status_filters = [
        "active",
        "completed",
        "blocked",
        "cancelled",
    ]

    def _status_tab(s):
        active = s == (status_filter or "")
        cls = "btn btn-sm" + ("" if active else " btn-secondary")
        return f"<a href='?page=1&amp;status={s}' class='{cls}'>{s.title()}</a>"

    all_tab_cls = "btn btn-sm" + ("" if not status_filter else " btn-secondary")
    status_tabs = f"<a href='?page=1' class='{all_tab_cls}'>All</a>" + "".join(
        _status_tab(s) for s in status_filters
    )

    act_extra_qs = f"status={quote(status_filter, safe='')}" if status_filter else ""
    activity_pagination_html = _build_pagination(page, total_pages, act_extra_qs)
    page_start_act = offset + 1 if total_activities else 0
    page_end_act = offset + len(activities)
    activity_page_info = f"Page {page} of {total_pages} &nbsp;·&nbsp; Showing {page_start_act}–{page_end_act} of {total_activities}"
    from app.services import waiting_service

    waiting = waiting_service.waiting_on_person(user_id=scope_user_id, include=("handoffs",), limit=20)["handoffs"]
    waiting_rows = "".join(
        f"<tr><td><div style='white-space:pre-line'>{escape_html(w.get('needs_human') or '')}</div>"
        f"<div class='text-muted' style='font-size:0.8rem'>From {escape_html(agent_labels.get(w['agent_id'], w['agent_id']))}"
        f" · {escape_html(w.get('task_description', '')[:100])}</div></td>"
        f"<td>{escape_html(_scope_words(w.get('memory_scope') or '', workspace_names, agent_labels))}</td>"
        f"<td>{local_dt(w.get('updated_at'), style='date')}</td>"
        f"<td><button type='button' class='btn btn-sm' data-handoff-resolve='{escape_html(w['id'])}'>Mark resolved</button></td></tr>"
        for w in waiting
    )
    superseded = activity_service.list_superseded_requests(user_id=scope_user_id)
    superseded_rows = "".join(
        f"<tr><td><div style='white-space:pre-line'>{escape_html(w.get('needs_human') or '')}</div>"
        f"<div class='text-muted' style='font-size:0.8rem'>From {escape_html(agent_labels.get(w['agent_id'], w['agent_id']))}"
        f" · {escape_html((w.get('task_description') or '')[:100])}</div></td>"
        f"<td>{escape_html(_scope_words(w.get('memory_scope') or '', workspace_names, agent_labels))}</td>"
        f"<td><code>{escape_html(w.get('superseded_by') or '')}</code></td>"
        f"<td>{local_dt(w.get('handoff_resolved_at'), style='date')}</td></tr>"
        for w in superseded
    )
    waiting_table = (
        "<div style='overflow-x:auto'><table><thead><tr><th>Request</th><th>Scope</th><th>Since</th><th></th></tr></thead>"
        f"<tbody>{waiting_rows}</tbody></table></div>"
        if waiting
        else "<p class='text-muted' style='font-size:0.85rem'>Nothing is waiting on you.</p>"
    )
    superseded_group = (
        "<details class='handoff-superseded' style='margin-top:12px'>"
        f"<summary>Superseded by a later handoff ({len(superseded)})</summary>"
        "<p class='text-muted' style='font-size:0.85rem'>The agent that asked made a new request in a later session in the "
        "same workspace, which replaced these. It was shown them so it could restate any that still apply.</p>"
        "<div style='overflow-x:auto'><table><thead><tr><th>Request</th><th>Scope</th><th>Replaced by</th><th>When</th></tr></thead>"
        f"<tbody>{superseded_rows}</tbody></table></div></details>"
        if superseded
        else ""
    )
    waiting_card = (
        "<div class='card'><h2>Waiting on you</h2>"
        "<p class='text-muted' style='font-size:0.85rem'>Agents flagged these in a handoff as needing a person. "
        "Each one appears at the top of every agent session in its workspace until you mark it resolved, "
        "the agent that asked resolves it, or a request that agent makes in a later session in the same workspace replaces it.</p>"
        f"{waiting_table}{superseded_group}</div>"
        if waiting or superseded
        else ""
    )

    def render_row(a):
        aid = escape_html(a["id"])
        status = a.get("status", "")
        # Only the actions that apply; one is a plain button, two or more go under More.
        extra = []
        if status in ("stale", "blocked", "reassigned"):
            extra.append(f"<button type='button' class='btn btn-sm btn-secondary' data-activity-start='{aid}' title='Start'>Start</button>")
        if status in ("active", "stale", "blocked", "reassigned"):
            extra.append(f"<button type='button' class='btn btn-sm btn-secondary' data-activity-cancel='{aid}' title='Cancel'>Cancel</button>")
        if len(extra) > 1:
            more = (
                "<details class='row-more'><summary class='btn btn-sm btn-secondary' title='Start or cancel this task'>More</summary>"
                f"<div class='row-more-list'>{''.join(extra)}</div></details>"
            )
        else:
            more = "".join(extra)
        done_btn = (
            f"<button type='button' class='btn btn-sm btn-secondary' data-activity-done='{aid}' data-activity-status='{escape_html(status)}' title='Done'>Done</button>"
            if status in ("active", "stale", "blocked", "reassigned")
            else ""
        )
        return (
            f"<tr class='activity-row' data-status='{escape_html(status)}'>"
            f"<td class='activity-task-cell'>{_task_cell(a)}</td>"
            f"<td class='activity-scope-cell' data-label='Workspace' title='{escape_html(a.get('memory_scope') or '')}'>{escape_html(_scope_words(a.get('memory_scope') or '', workspace_names, agent_labels))}</td>"
            f"<td class='activity-status-cell' data-label='Status'><span class='badge badge-{escape_html(status or 'active')}'>{escape_html(status)}</span>{_ask_note(a)}</td>"
            f"<td class='activity-agent-cell' data-label='Agent'>{escape_html(agent_labels.get(a.get('assigned_agent_id', ''), a.get('assigned_agent_id', '')))}</td>"
            f"<td class='activity-updated-cell' data-label='Updated'>{local_dt(a.get('updated_at'), style='date')}<br>{local_dt(a.get('updated_at'), style='time')}</td>"
            f"<td><div class='actions-cell activity-actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' data-activity-detail='{aid}'>Detail</button>"
            f"{done_btn}{more}"
            f"</div></td></tr>"
        )

    rows = "".join(render_row(a) for a in activities)

    focus_html = ""
    focus_id = request.query_params.get("task", "")
    if focus_id:
        focus = activity_service.get_activity(focus_id)
        if focus and (is_admin or focus.get("user_id") == session["user_id"]):
            focus_html = (
                '<section class="card" id="activity-focus"><h2>This task</h2>'
                '<table class="activity-table"><thead><tr><th>Task</th><th>Workspace</th><th>Status</th><th>Agent</th><th>Updated</th><th></th></tr></thead>'
                f"<tbody>{render_row(focus)}</tbody></table></section>"
            )

    import json as _json

    detail_source = list(activities) + ([focus] if focus_id and focus_html and focus else [])
    detail_data = {a["id"]: _detail_record(a, workspace_names, agent_labels) for a in detail_source}
    detail_blob = (
        '<script type="application/json" id="activity-detail-data">'
        + _json.dumps(detail_data).replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026").replace("'", "\\u0027")
        + "</script>"
    )
    detail_modal = """
    <div class="modal-overlay" id="activity-detail-modal" style="display:none">
      <div class="modal modal-detail">
        <h2 id="activity-detail-title">Task</h2>
        <dl class="mem-facts" id="activity-detail-facts"></dl>
        <div id="activity-detail-sections"></div>
        <div class="modal-footer" id="activity-detail-footer"><button type="button" class="btn btn-secondary" onclick="closeModal('activity-detail-modal')">Close</button></div>
      </div>
    </div>"""

    js = """
    <script>
    async function refreshActivity() {
      const params = new URLSearchParams(window.location.search);
      window.location.href = '/activity?' + params.toString();
    }
    window.onAgentCoreEvent = function(event) {
      if (!(event.type || '').startsWith('activity_')) return;
      var existing = document.getElementById('activity-live-banner');
      if (existing) return;
      var banner = document.createElement('div');
      banner.id = 'activity-live-banner';
      banner.className = 'alert';
      banner.style.cssText = 'margin:0 0 12px;display:flex;align-items:center;gap:10px';
      banner.innerHTML = '<span>Activity updated in the background.</span>' +
        '<button class="btn btn-sm btn-secondary" onclick="location.reload()">Refresh</button>';
      var table = document.querySelector('.activity-table');
      if (table && table.parentNode) table.parentNode.insertBefore(banner, table);
    };
    // Row actions carry the activity id as data. Ids here are server-generated,
    // but the shape is the point: a value inside an inline handler is JavaScript
    // source, and the next field rendered that way may not be server-generated.
    document.addEventListener('click', async function(ev) {
      const start = ev.target.closest('[data-activity-start]');
      if (start) { ev.preventDefault(); updateActivity(start.dataset.activityStart, 'active'); return; }
      const done = ev.target.closest('[data-activity-done]');
      if (done) {
        ev.preventDefault();
        if (done.dataset.activityStatus === 'blocked') {
          // A blocked task is closed with a reason, which is recorded on it.
          const why = await promptDialog('It is recorded on the task.', { title: 'Why is this done?', input: { value: 'Done; closed from the dashboard' }, confirmLabel: 'Mark done' });
          if (why && why.trim()) updateActivity(done.dataset.activityDone, 'completed', why.trim());
          return;
        }
        updateActivity(done.dataset.activityDone, 'completed');
        return;
      }
      const cancel = ev.target.closest('[data-activity-cancel]');
      if (cancel) { ev.preventDefault(); cancelActivity(cancel.dataset.activityCancel); return; }
      const detail = ev.target.closest('[data-activity-detail]');
      if (detail) { ev.preventDefault(); openActivityDetail(detail.dataset.activityDetail); return; }
      const resolve = ev.target.closest('[data-handoff-resolve]');
      if (resolve) { ev.preventDefault(); resolveHandoff(resolve.dataset.handoffResolve); }
    });

    // The shared detail dialog: what the task is, then what it concluded, each part only when there is something to show.
    let ACTIVITY_DETAIL = null;
    function activityDetailData() {
      if (ACTIVITY_DETAIL === null) {
        const el = document.getElementById('activity-detail-data');
        try { ACTIVITY_DETAIL = el ? JSON.parse(el.textContent) : {}; } catch (e) { ACTIVITY_DETAIL = {}; }
      }
      return ACTIVITY_DETAIL;
    }
    function detailSection(label, bodyHtml) {
      return '<h3 class="sub-heading form-section-title">' + label + '</h3>' + bodyHtml;
    }
    function detailList(items) {
      return '<ul>' + items.map(function(i) { return '<li>' + escapeHtml(i) + '</li>'; }).join('') + '</ul>';
    }
    function openActivityDetail(id) {
      const d = activityDetailData()[id];
      if (!d) return;
      setModalTitle('activity-detail-modal', d.title || 'Task');
      const facts = [
        ['Status', '<span class="badge badge-' + escapeHtml(d.status || 'active') + '">' + escapeHtml(d.status || '') + '</span>'],
        ['Agent', escapeHtml(d.agent || '')],
        ['Workspace', escapeHtml(d.workspace || '')],
        ['Started', localDt(d.started_at)],
        ['Last update', localDt(d.updated_at)],
      ];
      if (d.task_ref) facts.push(['Task link', escapeHtml(d.task_ref)]);
      document.getElementById('activity-detail-facts').innerHTML = facts.map(function(f) {
        return '<div><dt>' + f[0] + '</dt><dd>' + f[1] + '</dd></div>';
      }).join('');
      const parts = [];
      if (d.result) parts.push(detailSection('Result', '<div class="mem-detail-content">' + escapeHtml(d.result) + '</div>'));
      if (d.note) parts.push(detailSection('Latest note', '<div class="mem-detail-content">' + escapeHtml(d.note) + '</div>'));
      if (d.next_steps.length) parts.push(detailSection('Next steps', detailList(d.next_steps)));
      if (d.open_questions.length) parts.push(detailSection('Open questions', detailList(d.open_questions)));
      if (!d.needs_human && d.ask_note) {
        parts.push(detailSection('Needs you', '<p class="form-hint">' + escapeHtml(d.ask_note) + '</p>'));
      }
      if (d.needs_human) {
        let needs = '<div class="mem-detail-content">' + escapeHtml(d.needs_human) + '</div>';
        if (d.waiting) {
          if (d.needs_url) {
            let host = d.needs_url;
            try { host = new URL(d.needs_url).hostname; } catch (e) {}
            needs += '<p><a class="btn" href="' + escapeHtml(d.needs_url) + '" target="_blank" rel="noopener noreferrer">Open in ' + escapeHtml(host) + '</a></p>' +
              '<div class="form-hint">Or tell ' + escapeHtml(d.agent) + ' here:</div>';
          }
          needs += '<form class="inline-answer" id="activity-answer-form" data-answer-for="' + escapeHtml(d.id) + '">' +
            '<input type="text" maxlength="1000" placeholder="Your answer (optional)" aria-label="Your answer"> ' +
            '<button class="btn btn-sm btn-secondary" type="submit" data-empty-label="Mark as dealt with" data-typed-label="Send answer">Mark as dealt with</button></form>' +
            '<p class="form-hint">Your answer goes to ' + escapeHtml(d.agent) + ' the next time it works' +
            (/^(private to |personal$)/.test(d.workspace || '') ? '' : ' in ' + escapeHtml(d.workspace)) + ', and this closes.</p>';
        } else if (d.ask_note) {
          needs += '<p class="form-hint">' + escapeHtml(d.ask_note) + '</p>';
        }
        parts.push(detailSection('Needs you', needs));
      }
      document.getElementById('activity-detail-sections').innerHTML = parts.join('');
      // Footer: Close stays; this task's actions follow it (Cancel and Start quiet, Done the main one).
      const footer = document.getElementById('activity-detail-footer');
      footer.querySelectorAll('.dyn-action').forEach(function(b) { b.remove(); });
      function add(cls, attr, label, status) {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn ' + cls + ' dyn-action';
        b.setAttribute(attr, d.id);
        if (status !== undefined) b.setAttribute('data-activity-status', status);
        b.textContent = label;
        footer.appendChild(b);
      }
      if (d.can_start) add('btn-secondary', 'data-activity-start', 'Start');
      if (d.can_cancel) add('btn-secondary', 'data-activity-cancel', 'Cancel task');
      if (d.can_done) add('', 'data-activity-done', 'Done', d.status);
      openModal('activity-detail-modal');
    }
    document.addEventListener('input', function(ev) {
      const box = ev.target.closest && ev.target.closest('#activity-answer-form input');
      if (!box) return;
      const button = box.form.querySelector('button');
      button.textContent = box.value.trim() ? button.dataset.typedLabel : button.dataset.emptyLabel;
    });
    document.addEventListener('submit', async function(ev) {
      const form = ev.target.closest && ev.target.closest('#activity-answer-form');
      if (!form) return;
      ev.preventDefault();
      const answer = form.querySelector('input').value.trim();
      const j = await apiFetch('/api/activity/' + encodeURIComponent(form.dataset.answerFor) + '/handoff/resolve',
        { method: 'POST', body: JSON.stringify(answer ? { answer: answer } : {}) });
      if (j.ok) { showToast('Done'); refreshActivity(); }
      else { showToast((j.error && j.error.message) || 'Failed', 'danger'); }
    });

    async function resolveHandoff(id) {
      const j = await apiFetch('/api/activity/' + id + '/handoff/resolve', { method: 'POST' });
      if (j.ok) { showToast('Resolved'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }

    async function updateActivity(id, status, taskResult) {
      const body = { status };
      if (taskResult) body.task_result = taskResult;
      const j = await apiFetch('/api/activity/' + id, { method: 'PUT', body: JSON.stringify(body) });
      if (j.ok) { showToast('Updated'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function cancelActivity(id) {
      if (!await confirmDialog('Cancel this activity?')) return;
      const j = await apiFetch('/api/activity/' + id, { method: 'DELETE' });
      if (j.ok) { showToast('Cancelled'); refreshActivity(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    function copyGeneratedOutput(btn) {
      copyToClipboard(document.querySelector('#ig-output pre').textContent, btn);
    }

    </script>"""
    js = js.replace("window.onAgentCoreEvent", "window." + JS_WINDOW_EVENT)

    return render_page(
        "Activity",
        f"""
    <div class="page-header"><h1>Activity</h1></div>
    {detail_blob}{detail_modal}
    {focus_html}
    {waiting_card}
    <div class="card">
      <h2>Tasks</h2>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">What your agents are doing and have done. They open and close their own tasks.</p>
      <div class="filter-bar" style="margin-bottom:12px">
        {status_tabs}
      </div>
      <div style="overflow-x:auto">
      <table class="activity-table stack-phone"><thead><tr><th class="activity-task-cell">Task</th><th class="activity-scope-cell">Workspace</th><th class="activity-status-cell">Status</th><th class="activity-agent-cell">Agent</th><th class="activity-updated-cell">Updated</th><th class="activity-actions-header">Actions</th></tr></thead>
      <tbody>{rows or "<tr><td colspan=7 class=empty>No tasks yet.</td></tr>"}</tbody></table>
      </div>
      <div style="margin-top:8px;font-size:0.85rem;color:var(--text-muted)">{activity_page_info}</div>
      {activity_pagination_html}
    </div>


    """,
        "/activity",
        js,
        session=session,
    )
