"""Memory dashboard page."""

from collections import defaultdict

from fastapi import APIRouter, Request, Depends

from app.security.scope_enforcer import ScopeEnforcer
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    get_icon,
    _hf,
)

router = APIRouter()


def _cut_at_a_word(text: str, limit: int) -> str:
    """The first ``limit`` characters, ended at a word with an ellipsis, never mid-word."""
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if text[limit] != " " and " " in cut:
        cut = cut[: cut.rindex(" ")]
    return cut.rstrip(" ,;:-") + "…"


def _written_line(record: dict) -> str:
    """Who wrote a memory and when, quietly under its text."""
    import json

    try:
        provenance = json.loads(record.get("provenance_json") or "{}") or {}
    except ValueError:
        provenance = {}
    who = provenance.get("agent_id") if provenance.get("actor_type") == "agent" else ("the owner" if provenance.get("actor_type") == "user" else provenance.get("agent_id") or provenance.get("actor_id"))
    when = str(record.get("created_at") or "")[:10]
    text = " · ".join(p for p in (f"by {who}" if who else "", when) if p)
    return f"<br><span class='text-muted' style='font-size:0.75rem'>{escape_html(text)}</span>" if text else ""


def _state_cell(r, trace: dict | None = None) -> str:
    """What can settle a record: its anchor, when it has one.

    There is no confirmation status here: nobody is asked to confirm memories, so "never confirmed"
    would be a warning about a chore that no longer exists.
    """
    marks = []
    anchor = r.get("subject_anchor")
    if anchor:
        # An anchor names what *could* settle the record. Saying "checked against"
        # would claim a verification that may never have happened.
        marks.append(
            f"<span class='badge badge-active' title='Can be checked against "
            f"{escape_html(anchor)}'>anchored</span>"
        )
    if r.get("record_status") == "retracted":
        trace = trace or {}
        when = (trace.get("when") or r.get("status_changed_at") or "")[:10]
        by = trace.get("by_label") or trace.get("by")
        text = " ".join(p for p in ("Retracted", when, f"by {by}" if by else "") if p)
        marks.append(f"<span class='text-muted'>{escape_html(text)}</span>")
    return " ".join(marks) or "<span class='text-muted'>—</span>"


@router.get("/memory")
def memory_page(request: Request, session: dict = Depends(require_auth)):
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
        f'<option value="agent:{a["id"]}">Agent: {escape_html(a.get("display_name") or a["id"])} (private)</option>'
        for a in agents
    )

    workspaces = (
        workspace_service.list_workspaces()
        if session.get("role") == "admin"
        else workspace_service.list_workspaces(owner_user_id=session["user_id"])
    )
    project_options = "".join(
        f'<option value="workspace:{p["id"]}">Workspace: {escape_html(p.get("name") or p["id"])}</option>'
        for p in workspaces
    )
    user_scope = f"user:{session['user_id']}"
    user_scope_label = "Just me (personal)"

    visible_scopes = [user_scope] + [f"workspace:{p['id']}" for p in workspaces]
    enforcer = ScopeEnforcer(
        [user_scope] + [f"workspace:{p['id']}" for p in workspaces],
        [user_scope] + [f"workspace:{p['id']}" for p in workspaces],
        session["user_id"],
        is_admin=is_admin,
        active_workspace_ids=frozenset(
            p["id"]
            for p in workspaces
            if workspace_service.can_user_read_workspace(session["user_id"], p["id"])
        ),
    )

    def can_modify_record(record: dict) -> bool:
        return is_admin or enforcer.can_write(record.get("scope") or "")

    def list_visible_memory(record_status):
        if is_admin:
            with get_db() as conn:
                rows = conn.execute(
                    """
                    SELECT id, content, memory_class, scope, topic, confidence, importance,
                           source_kind, created_at, record_status, superseded_by_id,
                           supersedes_id, subject_anchor, last_confirmed_at, status_changed_at, provenance_json
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

    show = "retracted" if request.query_params.get("show") == "retracted" else "active"
    active_records = list_visible_memory("active")
    retracted_records = list_visible_memory("retracted")
    records = retracted_records if show == "retracted" else active_records
    retraction_traces = memory_service.retraction_traces([r["id"] for r in retracted_records]) if show == "retracted" else {}

    workspace_labels = {f"workspace:{w['id']}": (w.get('name') or w['id']) for w in workspaces}

    def _scope_words(scope):
        """Where a memory lives, in the words the detail dialog uses."""
        if scope in workspace_labels:
            return workspace_labels[scope]
        if scope.startswith("workspace:"):
            return scope.replace("workspace:", "")
        if scope.startswith("agent:"):
            return "private to " + scope.replace("agent:", "")
        if scope.startswith("user:"):
            return "personal (the owner only)"
        return "shared with everyone" if scope == "shared" else scope

    def memory_row(r):
        """One row for either view; the only difference is Retract on an active memory and Restore on a retracted one."""
        delete_button = (
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-memory-delete='{escape_html(r['id'])}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')} Delete</button>"
        )
        status_button = (
            f"<button type='button' class='btn btn-sm btn-secondary' data-memory-restore='{escape_html(r['id'])}'>Restore</button>"
            if show == "retracted"
            else f"<button type='button' class='btn btn-sm btn-secondary' data-memory-retract='{escape_html(r['id'])}'>Retract</button>"
        )
        modify_buttons = (
            status_button + delete_button if can_modify_record(r) else "<span class='text-muted'>Read only</span>"
        )
        return (
            f"<tr data-scope='{escape_html(r.get('scope') or '')}'><td class='mem-class'><span class='badge badge-{escape_html(r.get('memory_class', ''))}'>{escape_html(r.get('memory_class', ''))}</span></td>"
            f"<td class='mem-content'>{escape_html(_cut_at_a_word(r.get('content', ''), 160))}{_written_line(r)}</td>"
            f"<td class='mem-ws'>{escape_html(_scope_words(r.get('scope') or ''))}</td>"
            f"<td class='mem-state'>{_state_cell(r, retraction_traces.get(r['id']))}</td>"
            f"<td class='mem-actions'><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' data-memory-detail='{escape_html(r['id'])}'>Detail</button>"
            f"{modify_buttons}"
            f"</div></td></tr>"
        )

    from app.routes.dashboard_shared import _build_pagination

    ws_scopes = sorted({r.get("scope") for r in records if r.get("scope")})
    ws_filter = request.query_params.get("workspace", "")
    shown_records = [r for r in records if not ws_filter or r.get("scope") == ws_filter]
    PAGE_SIZE = 25
    total_pages = max(1, (len(shown_records) + PAGE_SIZE - 1) // PAGE_SIZE)
    try:
        page = min(max(1, int(request.query_params.get("page", "1"))), total_pages)
    except ValueError:
        page = 1
    page_records = shown_records[(page - 1) * PAGE_SIZE : page * PAGE_SIZE]
    from urllib.parse import quote as _quote

    extra_query = "&".join(
        part for part in (f"show=retracted" if show == "retracted" else "", f"workspace={_quote(ws_filter, safe='')}" if ws_filter else "") if part
    )
    memory_pagination_html = _build_pagination(page, total_pages, extra_query) if total_pages > 1 else ""
    memory_page_info = (
        f"Page {page} of {total_pages} &nbsp;·&nbsp; Showing "
        f"{(page - 1) * PAGE_SIZE + 1 if shown_records else 0}–{(page - 1) * PAGE_SIZE + len(page_records)} of {len(shown_records)}"
    )
    records_rows = "".join(memory_row(r) for r in page_records) or (
        "<tr><td colspan=5 class=empty>No retracted memories.</td></tr>"
        if show == "retracted"
        else "<tr><td colspan=5 class=empty>No memories yet. Agents save them as they work, or use <strong>Save Memory</strong> above.</td></tr>"
    )
    def _view_tab(label, href, on):
        return f'<a href="{href}" class="page-tab{" active" if on else ""}"{current if on else ""}>{label}</a>'

    current = ' aria-current="page"'
    switch_html = (
        '<nav class="page-tabs" aria-label="Which memories">'
        + _view_tab(f"Active ({len(active_records)})", "/memory", show != "retracted")
        + _view_tab(f"Retracted ({len(retracted_records)})", "/memory?show=retracted", show == "retracted")
        + "</nav>"
    )
    # Retracted is a look-back: saving would make an active memory and search covers active ones only, so no header actions.
    save_button = "" if show == "retracted" else '<button class="btn" onclick="openModal(\'write-memory-modal\')">+ Save Memory</button>'
    search_button = (
        "" if show == "retracted" else '<button class="btn btn-secondary" onclick="toggleFilters()">Search</button>'
    )
    from app.services import system_settings_service

    retracted_days = system_settings_service.read_int("retracted_retention_days", 30)
    scratchpad_days = system_settings_service.read_int("scratchpad_retention_days", 7)

    def days_text(n: int) -> str:
        return f"{n} day" + ("" if n == 1 else "s")

    # The number is the live setting; for an admin it links to the field that sets it.
    retracted_days_html = (
        f'<a href="/settings#retracted-retention-days">{days_text(retracted_days)}</a>'
        if is_admin
        else days_text(retracted_days)
    )
    if show == "retracted":
        list_heading = "Retracted memories"
        list_hint = (
            f"Hidden from search and kept for {retracted_days_html}, then deleted for good. Restore one to make it active again, or delete it now. "
            "Detail shows when it was retracted, by whom or which rule, and when it will be deleted."
        )
    else:
        list_heading = "Memories"
        list_hint = "The memories you can read right now. Use Search for a narrower view, or pick a workspace."

    workspace_filter_html = (
        '<div class="form-group" style="max-width:320px;margin-bottom:10px"><label for="mem-ws-filter">Workspace</label>'
        '<select id="mem-ws-filter" onchange="applyMemoryWorkspaceFilter()"><option value="">All workspaces</option>'
        + "".join(
            f'<option value="{escape_html(sc)}"{" selected" if sc == ws_filter else ""}>{escape_html(_scope_words(sc))}</option>'
            for sc in ws_scopes
        )
        + "</select></div>"
        if len(ws_scopes) > 1
        else ""
    )

    js = (
        """
    <script>
    async function refreshMemory() { setTimeout(() => location.reload(), 150); }
    function memoryLabel(id) {
      const b = document.querySelector('[data-memory-detail="' + id + '"]');
      const row = b && b.closest('tr');
      const text = row && row.cells[1] ? row.cells[1].textContent.trim().replace(/\\s+/g, ' ') : '';
      return text ? ' "' + (text.length > 60 ? text.slice(0, 57) + '...' : text) + '"' : '';
    }
    async function retractRecord(id) {
      if (!await confirmDialog('Retract this memory' + memoryLabel(id) + '? It will be hidden from search and can be restored for ' + RETRACTED_DAYS + (RETRACTED_DAYS === 1 ? ' day' : ' days') + ', then it is deleted for good.')) return;
      const j = await apiFetch('/api/memory/retract?record_id=' + id, { method: 'POST' });
      if (j.ok) { showToast('Retracted'); refreshMemory(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function restoreRecord(id) {
      if (!await confirmDialog('Restore this memory' + memoryLabel(id) + '?')) return;
      const j = await apiFetch('/api/memory/restore?record_id=' + id, { method: 'POST' });
      if (j.ok) { showToast('Restored'); refreshMemory(); }
      else { showToast(j.error.message || 'Failed', 'danger'); }
    }
    async function deleteRecord(id) {
      if (!await confirmDialog('Permanently delete this memory' + memoryLabel(id) + '? This cannot be undone.')) return;
      const j = await apiFetch('/api/memory/' + id, { method: 'DELETE' });
      if (j.ok) { 
        showToast('Deleted', 'success'); 
        refreshMemory(); 
      } else { 
        showToast(j.error?.message || 'Failed to delete record', 'danger'); 
      }
    }
    function scopeLabel(scope) {
      if (scope.startsWith('workspace:')) return WORKSPACE_NAMES[scope] || scope.replace('workspace:', '');
      if (scope.startsWith('agent:')) return 'private to ' + scope.replace('agent:', '');
      if (scope.startsWith('user:')) return 'personal (the owner only)';
      return scope === 'shared' ? 'shared with everyone' : scope;
    }
    async function viewMemory(id) {
      const j = await apiFetch('/api/memory/' + id);
      if (!j.ok) { showToast(j.error.message || 'Failed', 'danger'); return; }
      const r = j.data.record;
      const contentText = r.content || '';
      document.getElementById('mem-detail-content').textContent = contentText;
      // The title is what the memory is about: its topic, else the start of what it says.
      setModalTitle('memory-detail-modal', r.topic || (contentText.length > 70 ? contentText.slice(0, 67).trim() + '...' : contentText) || 'Memory');
      document.getElementById('mem-detail-class').textContent = r.memory_class || '';
      document.getElementById('mem-detail-scope').textContent = scopeLabel(r.scope || '');
      const topicRow = document.getElementById('mem-detail-topic-row');
      document.getElementById('mem-detail-topic').textContent = r.topic || '';
      topicRow.style.display = r.topic ? '' : 'none';
      document.getElementById('mem-detail-created').innerHTML = localDt(r.created_at);
      let statusText = r.record_status || '';
      if (r.record_status === 'retracted') {
        const t = r.retraction || {};
        const when = (t.when || r.status_changed_at || '').substring(0, 10);
        statusText = 'retracted' + (when ? ' ' + when : '') + ((t.by_label || t.by) ? ' by ' + (t.by_label || t.by) : '');
        const stamp = t.when || r.status_changed_at || '';
        const ms = Date.parse(String(stamp).replace(' ', 'T') + (/[zZ]|[+-]\\d\\d:?\\d\\d$/.test(String(stamp)) ? '' : 'Z'));
        if (!isNaN(ms)) {
          const gone = new Date(ms + RETRACTED_DAYS * 86400000);
          statusText += '. Deleted for good on ' + gone.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' });
        }
        const extra = (t.duplicate_of ? 'Judged the same statement as record ' + t.duplicate_of + '. ' : '') + (t.reason ? 'Reason: ' + t.reason + '.' : '');
        if (extra) statusText += ' (' + extra.trim() + ')';
      }
      document.getElementById('mem-detail-status').textContent = statusText;
      // Shown only when set: a row that says "not set" is noise.
      document.getElementById('mem-detail-slot-key').textContent = r.slot_key || '';
      document.getElementById('mem-detail-slot-row').style.display = r.slot_key ? '' : 'none';
      document.getElementById('mem-detail-valid-from').innerHTML = r.valid_from ? localDt(r.valid_from) : '';
      document.getElementById('mem-detail-from-row').style.display = r.valid_from ? '' : 'none';
      document.getElementById('mem-detail-valid-to').textContent = r.valid_to ? r.valid_to.substring(0, 10) : '';
      document.getElementById('mem-detail-to-row').style.display = r.valid_to ? '' : 'none';
      document.getElementById('mem-detail-optional').style.display = (r.slot_key || r.valid_from || r.valid_to) ? '' : 'none';
      // "How sure": only what exists. A fact with no anchor says so (that is the point); a never-confirmed record says nothing.
      document.getElementById('mem-detail-last-confirmed').innerHTML = r.last_confirmed_at ? escapeHtml(r.last_confirmed_at.substring(0, 10)) : '';
      document.getElementById('mem-detail-confirmed-row').style.display = r.last_confirmed_at ? '' : 'none';
      const anchorText = r.subject_anchor || (r.memory_class === 'fact' ? 'nothing, so it cannot be checked automatically' : '');
      document.getElementById('mem-detail-anchor').textContent = anchorText;
      document.getElementById('mem-detail-anchor-row').style.display = anchorText ? '' : 'none';
      document.getElementById('mem-detail-sure').style.display = (r.last_confirmed_at || anchorText) ? '' : 'none';
      const provenanceEl = document.getElementById('mem-detail-provenance');
      let who = '';
      if (r.provenance_json) {
        try {
          const p = JSON.parse(r.provenance_json);
          const owner = p.actor_type === 'user';
          const name = owner ? 'the owner' : (p.agent_id || p.actor_id || 'an agent');
          const channel = {mcp: 'through MCP', api: 'through the API', dashboard: 'typed in the dashboard'}[p.channel] || '';
          const kind = r.source_kind || p.source_kind;
          // One phrase, not two: how it was made, or the agent's own words for it.
          const detail = kind === 'agent_inference' ? 'from its own observation'
            : kind === 'external_import' ? 'imported'
            : channel;
          const how = detail ? ', ' + detail : '';
          who = name + how;
        } catch (err) {
          who = '';
        }
      }
      provenanceEl.textContent = who || 'unknown';
      const supersedeEl = document.getElementById('mem-detail-supersede');
      if (r.superseded_by_id) {
        supersedeEl.textContent = 'A newer version replaced this one (' + r.superseded_by_id.substring(0, 12) + '...).';
        supersedeEl.style.display = 'block';
      } else {
        supersedeEl.style.display = 'none';
      }
      document.getElementById('mem-detail-id').value = id;
      // Only an active record can be confirmed; the button is hidden for retracted or replaced ones.
      const confirmBtn = document.getElementById('mem-detail-confirm');
      if (confirmBtn) {
        confirmBtn.dataset.memoryConfirm = id;
        confirmBtn.style.display = (r.record_status === 'active') ? '' : 'none';
      }
      // Earlier versions stay hidden until there are some; the current one is already shown above.
      const historyEl = document.getElementById('mem-history');
      historyEl.style.display = 'none';
      historyEl.open = false;
      const chainEl = document.getElementById('mem-chain-content');
      chainEl.innerHTML = '';
      openModal('memory-detail-modal');
      _loadChain(id, chainEl);
    }
    async function _loadChain(id, el) {
      const j = await apiFetch('/api/memory/' + id + '/chain');
      const historyEl = document.getElementById('mem-history');
      if (!j.ok) return;
      // The current version is the one shown above; list only the versions it replaced, newest first.
      const earlier = (j.data.chain || []).filter(function(r) { return r.id !== id && (r.content || '') !== (document.getElementById('mem-detail-content').textContent || ''); });
      if (!earlier.length) return;
      document.getElementById('mem-history-summary').textContent = earlier.length === 1 ? '1 earlier version' : earlier.length + ' earlier versions';
      el.innerHTML = earlier.map(function(r) {
        const metadataParts = [];
        if (r.topic) metadataParts.push(escapeHtml(r.topic));
        if (r.record_status) metadataParts.push(escapeHtml(r.record_status));
        if (r.created_at) metadataParts.push(localDt(r.created_at));
        return '<div class="mem-version">' +
          '<div class="text-muted" style="font-size:0.8rem;margin-bottom:4px">' + metadataParts.join(' · ') + '</div>' +
          '<div style="font-size:0.9rem;line-height:1.4">' + escapeHtml(r.content || '') + '</div>' +
          '</div>';
      }).join('');
      historyEl.style.display = '';
    }
    async function doSearch() {
      const query = document.getElementById('mem-query').value;
      const scope = document.getElementById('mem-scope').value;
      const memClass = document.getElementById('mem-class').value;
      const topic = document.getElementById('mem-search-topic').value.trim();
      const body = { query, limit: 50 };
      if (scope) body.scope = scope;
      if (memClass) body.memory_class = memClass;
      if (topic) body.topic = topic;
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
	          topic: document.getElementById('mem-topic').value || null,
	          importance: parseFloat(document.getElementById('mem-importance').value) || 0.5,
	          source_kind: 'operator_authored',
	          slot_key: document.getElementById('mem-slot-key').value.trim() || null,
	          valid_from: document.getElementById('mem-valid-from').value || null,
	          valid_to: document.getElementById('mem-valid-to').value || null,
	        };
        const j = await apiFetch('/api/memory/write', { method: 'POST', body: JSON.stringify(body) });
        if (j.ok) { showToast('Written'); closeModal('write-memory-modal'); refreshMemory(); }
        else { showToast(j.error.message || 'Failed', 'danger'); }
      } catch (err) {
        console.error('Memory write failed', err);
        showToast('Memory write failed: ' + (err.message || err), 'danger');
      }
    }
    function applyMemoryWorkspaceFilter() {
      const sel = document.getElementById('mem-ws-filter');
      const want = sel ? sel.value : '';
      const parts = [];
      if (new URLSearchParams(window.location.search).get('show') === 'retracted') parts.push('show=retracted');
      if (want) parts.push('workspace=' + encodeURIComponent(want));
      window.location.href = '/memory' + (parts.length ? '?' + parts.join('&') : '');
    }
    function displayRecords(records) {
      const tbody = document.getElementById('mem-results-body');
      if (!records.length) { tbody.innerHTML = '<tr><td colspan=5 class=empty>No memories found.</td></tr>'; return; }
      tbody.innerHTML = records.map(r => `
        <tr data-scope="${escapeHtml(r.scope || '')}">
          <td class="mem-class"><span class="badge badge-${escapeHtml(r.memory_class)}">${escapeHtml(r.memory_class)}</span></td>
          <td class="mem-content">${escapeHtml((r.content || '').substring(0, 160))}</td>
          <td class="mem-ws">${escapeHtml(scopeLabel(r.scope || ''))}</td>
          <td class="mem-state">${(r.confidence || 0.5).toFixed(1)}</td>
          <td class="mem-actions"><button class="btn btn-sm btn-secondary" data-record-id="${escapeHtml(r.id)}" onclick="retractRecord(this.dataset.recordId)">Retract</button></td>
        </tr>`).join('');
    }
    function copyGeneratedOutput(btn) {
      copyToClipboard(document.querySelector('#ig-output pre').textContent, btn);
    }

    document.addEventListener('click', function(ev) {
      const retract = ev.target.closest('[data-memory-retract]');
      if (retract) { ev.preventDefault(); retractRecord(retract.dataset.memoryRetract); return; }
      const del = ev.target.closest('[data-memory-delete]');
      if (del) { ev.preventDefault(); deleteRecord(del.dataset.memoryDelete); return; }
      const detail = ev.target.closest('[data-memory-detail]');
      if (detail) { ev.preventDefault(); viewMemory(detail.dataset.memoryDetail); return; }
      const restore = ev.target.closest('[data-memory-restore]');
      if (restore) { ev.preventDefault(); restoreRecord(restore.dataset.memoryRestore); return; }
      const detailConfirm = ev.target.closest('[data-memory-confirm]');
      if (detailConfirm) {
        ev.preventDefault();
        confirmRecordWithEvidence(detailConfirm.dataset.memoryConfirm);
        return;
      }
    });

    async function confirmRecordWithEvidence(recordId) {
      const evidence = window.prompt(
        'What did you check? Say what you actually looked at, e.g. "read app/foo.py" or "ssh router: /etc/version = v3.0.1".'
      );
      if (!evidence || !evidence.trim()) {
        if (evidence !== null) showToast('Evidence is required to confirm', 'warning');
        return;
      }
      const confirmJ = await apiFetch('/api/memory/confirm', {
        method: 'POST', body: JSON.stringify({ record_id: recordId, evidence: evidence.trim() })
      });
      if (!confirmJ.ok) {
        showToast(confirmJ.error?.message || 'Confirm failed', 'danger');
        return;
      }
      showToast('Confirmed — ranking penalty cleared');
      // Close the detail modal if open, then reload.
      const detailModal = document.getElementById('memory-detail-modal');
      if (detailModal) closeModal('memory-detail-modal');
      refreshMemory();
    }
    function toggleFilters() {
      const f = document.getElementById('filter-panel');
      f.style.display = f.style.display === 'none' ? 'block' : 'none';
    }
    </script>"""
    )

    import json as _json

    workspace_names_js = (
        "<script>const WORKSPACE_NAMES = "
        + _json.dumps({f"workspace:{w['id']}": (w.get("name") or w["id"]) for w in workspaces}).replace("</", "<\\/")
        + ";</script>"
        + f"<script>const RETRACTED_DAYS = {int(retracted_days)};</script>"
    )
    js = workspace_names_js + js

    return render_page(
        "Memory",
        _hf(
            f"""
    <div class="page-header"><h1>Memory</h1><div class="page-actions">
        {save_button}
        {search_button}
    </div></div>

    <!-- Search Filters -->
    <div class="card" id="filter-panel" style="display:none">
      <h2>Search Memory</h2>
      <div class="filter-bar">
        <input type="text" id="mem-query" class="search-input" placeholder="Search query...">
        <select id="mem-scope" title="Where to search">
          <option value="">Everything I can read</option>
          <option value="{user_scope}">{user_scope_label}</option>
          {agent_options}
          {project_options}
          <option value="shared">Shared memory (everyone)</option>
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
          <input type="text" id="mem-search-topic" placeholder="Topic, e.g. docker">
        </div>
        <p class="form-hint">Use these only when you want to narrow the result set. Search always respects access first.</p>
      </details>
    </div>


    <!-- Memories -->
    {switch_html}
    <div class="card">
      <h2>{list_heading} <span id="mem-count" class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(records)})</span></h2>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">{list_hint}</p>
      {workspace_filter_html}
      <table class="stack-cards"><thead><tr><th>Kind</th><th>Content</th><th>Workspace</th><th>State</th><th class="actions-cell">Actions</th></tr></thead>
      <tbody id="mem-results-body">
        {records_rows}
      </tbody></table>
      <div id="mem-page-info" style="margin-top:8px;font-size:0.85rem;color:var(--text-muted)">{memory_page_info}</div>
      {memory_pagination_html}
      <input type="hidden" id="current-scope" value="{user_scope}">
    </div>


    <!-- Write Memory Modal -->
    <div class="modal-overlay" id="write-memory-modal" style="display:none">
      <div class="modal">
        <h2>Save Memory</h2>
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
              <p class="form-hint">Fact is the safe default. Use decision for chosen direction, preference for user/team preferences, and scratchpad for temporary notes (deleted after {days_text(scratchpad_days)}).</p>
            </div>
            <div class="form-group">
              <label>Save to *</label>
              <select id="mem-write-scope" required>
                <option value="{user_scope}" selected>{user_scope_label}</option>
                {agent_options}
                {project_options}
                <option value="shared">Shared memory (everyone)</option>
              </select>
              <p class="form-hint">Personal memory is the default. Workspaces are for shared team context; private memory is for one agent. Shared memory is PII-checked.</p>
            </div>
          </div>
          <details style="margin-top:4px">
            <summary>More options</summary>
            <div class="form-row" style="margin-top:10px">
              <div class="form-group">
                <label>Topic</label>
                <input type="text" id="mem-topic" placeholder="e.g. style" autocomplete="off">
                <p class="form-hint">Optional tag for searches and filtering.</p>
              </div>
              <div class="form-group">
                <label>Importance</label>
                <input type="number" id="mem-importance" value="0.7" min="0" max="1" step="0.1">
                <p class="form-hint">Higher values rank earlier when results are similar.</p>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label>Slot key</label>
                <input type="text" id="mem-slot-key" placeholder="e.g. style" autocomplete="off">
                <p class="form-hint">Optional. For preferences: saving a new value with the same slot key replaces the old one, so only one stays active.</p>
              </div>
            </div>
            <div class="form-row">
              <div class="form-group">
                <label>Valid From</label>
                <input type="datetime-local" id="mem-valid-from">
                <p class="form-hint">Optional. When this became true in the world. Leave blank if it always has been.</p>
              </div>
              <div class="form-group">
                <label>Valid To</label>
                <input type="datetime-local" id="mem-valid-to">
                <p class="form-hint">Optional. When it stops being true. Leave blank if it has no end.</p>
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

    <!-- Memory Detail Modal: what it says first, then a compact row of facts, then how sure and where it came from -->
    <div class="modal-overlay" id="memory-detail-modal" style="display:none">
      <div class="modal modal-detail">
        <h2 id="mem-detail-title">Memory</h2>
        <div id="mem-detail-content" class="mem-detail-content"></div>
        <div id="mem-detail-supersede" class="alert alert-warning" style="display:none"></div>
        <dl class="mem-facts">
          <div><dt>Kind</dt><dd><span id="mem-detail-class" class="badge"></span></dd></div>
          <div><dt>Visible to</dt><dd id="mem-detail-scope"></dd></div>
          <div><dt>Status</dt><dd><span id="mem-detail-status" class="badge"></span></dd></div>
          <div><dt>Created</dt><dd id="mem-detail-created"></dd></div>
          <div><dt>Written by</dt><dd id="mem-detail-provenance"></dd></div>
          <div id="mem-detail-topic-row"><dt>Topic</dt><dd id="mem-detail-topic"></dd></div>
        </dl>
        <div id="mem-detail-sure">
        <h3 class="sub-heading form-section-title">How sure</h3>
        <dl class="mem-facts">
          <div id="mem-detail-confirmed-row"><dt>Last confirmed</dt><dd id="mem-detail-last-confirmed"></dd></div>
          <div id="mem-detail-anchor-row"><dt>Can be checked against</dt><dd id="mem-detail-anchor"></dd></div>
        </dl>
        </div>
        <dl class="mem-facts" id="mem-detail-optional">
          <div id="mem-detail-slot-row"><dt>Replaces earlier notes filed under</dt><dd><code id="mem-detail-slot-key"></code></dd></div>
          <div id="mem-detail-from-row"><dt>True from</dt><dd id="mem-detail-valid-from"></dd></div>
          <div id="mem-detail-to-row"><dt>True until</dt><dd id="mem-detail-valid-to"></dd></div>
        </dl>
        <input type="hidden" id="mem-detail-id" value="">
        <details class="mem-more" id="mem-history" style="display:none">
          <summary id="mem-history-summary">Earlier versions</summary>
          <div id="mem-chain-content" style="font-size:0.85rem;margin-top:8px"></div>
        </details>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" onclick="closeModal('memory-detail-modal')">Close</button>
          <button type="button" class="btn" data-memory-confirm="" id="mem-detail-confirm">Confirm it is still true</button>
        </div>
      </div>
    </div>
    """
        ),
        "/memory",
        js,
        session=session,
    )
