"""Memory dashboard page."""

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
    enforcer = ScopeEnforcer(
        [user_scope] + [f"workspace:{p['id']}" for p in workspaces],
        [user_scope] + [f"workspace:{p['id']}" for p in workspaces],
        session["user_id"],
        is_admin=is_admin,
        active_workspace_ids=frozenset(
            p["id"] for p in workspaces if workspace_service.can_user_read_workspace(session["user_id"], p["id"])
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
                           source_kind, created_at, record_status, superseded_by_id, supersedes_id
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
        modify_buttons = (
            f"<button type='button' class='btn btn-sm btn-warning' data-memory-retract='{escape_html(r['id'])}'>Retract</button>"
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-memory-delete='{escape_html(r['id'])}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            if can_modify_record(r)
            else "<span class='text-muted'>Read only</span>"
        )
        return (
            f"<tr><td><span class='badge badge-{r.get('memory_class', '')}'>{r.get('memory_class', '')}</span></td>"
            f"<td>{escape_html(r.get('content', '')[:80])}</td>"
            f"<td><code>{(r.get('scope') or '').replace('workspace:', '')}</code></td>"
            f"<td>{r.get('confidence', 0.5):.1f}</td>"
            f"<td><div class='actions-cell'>"
            f"<button type='button' class='btn btn-sm btn-secondary' data-memory-detail='{escape_html(r['id'])}'>Detail</button>"
            f"{modify_buttons}"
            f"</div></td></tr>"
        )

    def retracted_row(r):
        modify_buttons = (
            f"<button type='button' class='btn btn-sm btn-secondary' data-memory-restore='{escape_html(r['id'])}'>Restore</button>"
            f"<button type='button' class='btn btn-sm btn-danger icon-delete-btn' data-memory-delete='{escape_html(r['id'])}' title='Permanently delete' aria-label='Permanently delete'>{get_icon('delete')}</button>"
            if can_modify_record(r)
            else "<span class='text-muted'>Read only</span>"
        )
        return (
            f"<tr style='opacity:0.65'><td><span class='badge badge-inactive'>{r.get('memory_class', '')}</span></td>"
            f"<td>{escape_html(r.get('content', '')[:80])}</td>"
            f"<td><code>{(r.get('scope') or '').replace('workspace:', '')}</code></td>"
            f"<td>{r.get('confidence', 0.5):.1f}</td>"
            f"<td><div class='actions-cell'>"
            f"{modify_buttons}"
            f"</div></td></tr>"
        )

    records_rows = (
        "".join(active_row(r) for r in active_records)
        or "<tr><td colspan=5 class=empty>No active records.</td></tr>"
    )
    retracted_rows = "".join(retracted_row(r) for r in retracted_records)

    # Consolidation review queue. Admin-only, and rendered server-side so the
    # card reflects real queue state on load rather than appearing empty until
    # a fetch lands.
    review_html = ""
    if is_admin:
        from app.services import memory_proposal_service

        pending = memory_proposal_service.list_proposals(status="pending", limit=200)
        stats = memory_proposal_service.rule_stats()

        def proposal_card(p: dict) -> str:
            """One suggestion, phrased as a question with buttons named for what they do.

            The reviewer needs three things and nothing else: the memory itself,
            why it was flagged, and what each button will do to it. Rule names
            and record ids are mechanism — kept available but out of the way.
            """
            evidence = p.get("evidence") or {}
            records = evidence.get("records") or []
            keep = evidence.get("keep")
            pid = escape_html(p["id"])

            memory_blocks = "".join(
                f'<blockquote style="margin:8px 0;padding:8px 12px;border-left:3px solid var(--border);'
                f'background:var(--bg-subtle)">{escape_html(r.get("content_preview") or "")}'
                f'<br><span class="text-muted" style="font-size:0.8rem">'
                f'{escape_html(r.get("topic") or "no topic")} · {escape_html(r.get("scope") or "")}'
                f' · <code>{escape_html(r.get("id") or "")}</code></span></blockquote>'
                for r in records
            )
            keep_line = (
                f'<p class="form-hint">The newer copy stays: '
                f'"{escape_html((keep.get("content_preview") or "")[:120])}"</p>'
                if keep
                else ""
            )

            # An anchor_missing card has a third honest answer: the memory is
            # fine and the pointer is wrong. Without it the only alternatives
            # are "retract a good memory" or "do nothing".
            reanchor = ""
            if p["rule"] == "anchor_missing":
                current = escape_html((p.get("evidence") or {}).get("anchor") or "")
                reanchor = f"""
        <div class="form-group" style="margin-top:10px">
          <label>Or point it at the right thing</label>
          <input type="text" id="anchor-{pid}" value="{current}" placeholder="repo:path/to/file.py" style="width:min(420px,100%)">
          <button class="btn btn-sm" data-proposal-reanchor="{pid}">Fix the pointer</button>
          <p class="form-hint">Keeps the memory and re-checks it against the new location. Use this when the file moved or the pointer was wrong — the memory itself may be perfectly good.</p>
        </div>"""

            keep_label = (
                "Yes, keep it" if p["rule"] == "anchor_missing" else "Yes, still current"
            )
            if p["action"] == "pin":
                wants_pin = (p.get("evidence") or {}).get("pin", True)
                verb = "Show it to every session" if wants_pin else "Stop showing it"
                buttons = f"""
            <button class="btn btn-sm btn-warning" data-proposal-id="{pid}" data-proposal-verdict="accepted">{verb}</button>
            <button class="btn btn-sm btn-secondary" data-proposal-id="{pid}" data-proposal-verdict="rejected">No</button>"""
                consequences = (
                    f"<strong>{verb}</strong> applies it now. <strong>No</strong> declines, and "
                    "the same request will not be raised again."
                )
            elif p["action"] == "confirm":
                buttons = f"""
            <button class="btn btn-sm" data-proposal-id="{pid}" data-proposal-verdict="accepted" data-proposal-outcome="still_current">{keep_label}</button>
            <button class="btn btn-sm btn-warning" data-proposal-id="{pid}" data-proposal-verdict="accepted" data-proposal-outcome="no_longer_current">Retract — out of date</button>
            <button class="btn btn-sm btn-warning" data-proposal-id="{pid}" data-proposal-verdict="accepted" data-proposal-outcome="not_useful">Retract — not worth keeping</button>
            <button class="btn btn-sm btn-secondary" data-proposal-id="{pid}" data-proposal-verdict="rejected">Leave it, don't ask again</button>"""
                consequences = (
                    "<strong>Yes, still current</strong> marks it as checked today. "
                    "Either <strong>Retract</strong> hides it from agents — it moves to "
                    "Retracted Records below, where you can restore it. Pick <strong>out of "
                    "date</strong> if the world moved on, or <strong>not worth keeping</strong> "
                    "if it is still accurate but too vague to act on. "
                    "<strong>Leave it</strong> keeps it and stops flagging it."
                )
            else:
                buttons = f"""
            <button class="btn btn-sm btn-warning" data-proposal-id="{pid}" data-proposal-verdict="accepted">Yes, retract it</button>
            <button class="btn btn-sm btn-secondary" data-proposal-id="{pid}" data-proposal-verdict="rejected">No, keep it</button>"""
                consequences = (
                    "<strong>Yes, retract it</strong> hides it from agents — it moves to "
                    "Retracted Records below, where you can restore it. "
                    "<strong>No, keep it</strong> leaves it alone and stops flagging it."
                )

            return f"""
      <div class="card" style="border-left:4px solid var(--text-muted);margin-bottom:12px" id="proposal-{pid}">
        <h4 style="margin:0 0 4px">{escape_html(p.get('prompt') or 'Review this memory')}</h4>
        <p class="text-muted" style="font-size:0.85rem;margin:0">{escape_html(p.get('rule_description') or '')}</p>
        {memory_blocks}
        <p style="margin:8px 0 4px">{escape_html(p['rationale'])}</p>
        {keep_line}
        <div class="actions-cell" style="margin-top:10px">{buttons}</div>
        <p class="form-hint" style="margin-top:6px">{consequences}</p>
        {reanchor}
      </div>"""

        def stat_row(s: dict) -> str:
            accuracy = "—" if s["precision"] is None else f"{s['precision']:.0%}"
            return (
                f"<tr><td>{escape_html(s['description'])}<br>"
                f"<span class='text-muted' style='font-size:0.8rem'><code>{escape_html(s['rule'])}</code></span></td>"
                f"<td>{s['pending']}</td><td>{s['accepted']}</td><td>{s['rejected']}</td>"
                f"<td>{accuracy}</td><td>{s['records_affected']}</td></tr>"
            )

        stat_rows = "".join(stat_row(s) for s in stats)

        proposals_html = (
            "".join(proposal_card(p) for p in pending)
            or '<p class="text-muted">Nothing to look at right now.</p>'
        )

        review_html = f"""
    <div class="card">
      <div style="display:flex;justify-content:space-between;align-items:center;gap:12px;flex-wrap:wrap">
        <h3 style="margin:0">Memory Clean-up <span id="proposal-count" class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(pending)} to look at)</span></h3>
        <button class="btn btn-secondary" onclick="generateProposals()">Check for more</button>
      </div>
      <p class="text-muted" style="font-size:0.85rem;margin:8px 0 12px">Memories that may no longer be worth keeping — one-off job logs, ticket notes, repeats, or details that go out of date. Each one asks you a question. Nothing changes until you answer, and anything hidden can be restored from Retracted Records below.</p>
      <div id="proposal-list">{proposals_html}</div>
      <details style="margin-top:12px">
        <summary class="text-muted" style="cursor:pointer">How good these suggestions have been</summary>
        <p class="form-hint">How often you have agreed with each kind of suggestion. This is what would justify letting any of them run automatically later — a kind you have not answered yet shows no score rather than a perfect one.</p>
        <table><thead><tr><th>Kind of suggestion</th><th>Waiting</th><th>Agreed</th><th>Disagreed</th><th>Agreed with</th><th>Memories changed</th></tr></thead>
        <tbody>{stat_rows}</tbody></table>
      </details>
    </div>
    """

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
      document.getElementById('mem-detail-topic').textContent = r.topic || '';
      document.getElementById('mem-detail-confidence').textContent = r.confidence != null ? r.confidence.toFixed(2) : '';
      document.getElementById('mem-detail-importance').textContent = r.importance != null ? r.importance.toFixed(2) : '';
      document.getElementById('mem-detail-created').innerHTML = localDt(r.created_at);
      document.getElementById('mem-detail-status').textContent = r.record_status || '';
      document.getElementById('mem-detail-slot-key').textContent = r.slot_key || '';
      document.getElementById('mem-detail-valid-from').innerHTML = r.valid_from ? localDt(r.valid_from) : '';
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
      // Reset history before loading fresh chain for this record
      const chainEl = document.getElementById('mem-chain-content');
      chainEl.innerHTML = '<span class="text-muted">Loading...</span>';
      openModal('memory-detail-modal');
      _loadChain(id, chainEl);
    }
    async function _loadChain(id, el) {
      const j = await apiFetch('/api/memory/' + id + '/chain');
      if (!j.ok) { el.innerHTML = '<span class="text-muted">Could not load history.</span>'; return; }
      const chain = j.data.chain || [];
      if (!chain.length) {
        el.innerHTML = '<div class="text-muted">No previous versions.</div>';
        return;
      }
      el.innerHTML = chain.map((r, i) => {
        const edge = i > 0 ? '<div class="text-muted" style="font-size:0.8rem;margin-bottom:2px">&larr; earlier version</div>' : '';
        const tag = i === chain.length - 1 ? 'Current' : 'Earlier';
        const metadataParts = [];
        if (r.topic) metadataParts.push(r.topic);
        if (r.record_status) metadataParts.push(r.record_status);
        if (r.created_at) metadataParts.push(localDt(r.created_at));
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
    }
    async function doSearch() {
      const query = document.getElementById('mem-query').value;
      const scope = document.getElementById('mem-scope').value;
      const memClass = document.getElementById('mem-class').value;
      const topic = document.getElementById('mem-search-topic').value.trim();
      const minConfidence = parseFloat(document.getElementById('mem-min-confidence').value);
      const body = { query, limit: 50 };
      if (scope) body.scope = scope;
      if (memClass) body.memory_class = memClass;
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
    async function doImport(e) {
      if (e && e.preventDefault) e.preventDefault();
      const importBtn = document.getElementById('mem-import-submit');
      const resultEl = document.getElementById('mem-import-result');
      const originalLabel = importBtn ? importBtn.textContent : '';
      if (importBtn) {
        importBtn.disabled = true;
        importBtn.textContent = 'Importing...';
      }
      if (resultEl) {
        resultEl.innerHTML = '<div class="alert alert-info">Importing notes...</div>';
      }
      try {
        const files = Array.from(document.getElementById('mem-import-files').files || []);
        const pasted = document.getElementById('mem-import-text').value.trim();
        const sources = [];
        for (const file of files) {
          sources.push({ filename: file.name, content: await file.text() });
        }
        if (pasted) {
          sources.push({
            filename: document.getElementById('mem-import-name').value.trim() || 'pasted-notes.txt',
            content: pasted
          });
        }
        if (!sources.length) {
          showToast('Choose files or paste text to import', 'warning');
          if (resultEl) resultEl.innerHTML = '<div class="alert alert-warning">Choose files or paste text to import.</div>';
          return;
        }
        const body = {
          scope: document.getElementById('mem-import-scope').value || '"""
        + user_scope
        + """',
          memory_class: document.getElementById('mem-import-class').value,
          topic: document.getElementById('mem-import-topic').value.trim() || null,
          confidence: parseFloat(document.getElementById('mem-import-confidence').value) || 0.85,
          importance: parseFloat(document.getElementById('mem-import-importance').value) || 0.6,
          sources
        };
        const j = await apiFetch('/api/memory/import', { method: 'POST', body: JSON.stringify(body) });
        if (j.ok) {
          const total = j.data.total_records || 0;
          const names = (j.data.imported || []).map(x => x.filename + ' (' + x.record_count + ')').join(', ');
          if (resultEl) resultEl.innerHTML = '<div class="alert alert-success">Imported ' + total + ' record' + (total === 1 ? '' : 's') + (names ? ': ' + escapeHtml(names) : '') + '</div>';
          showToast('Imported ' + total + ' memory record' + (total === 1 ? '' : 's'), 'success');
          refreshMemory();
        } else {
          showToast(j.error?.message || 'Import failed', 'danger');
          if (resultEl) resultEl.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Import failed') + '</div>';
        }
      } catch (err) {
        console.error('Memory import failed', err);
        showToast('Memory import failed: ' + (err.message || err), 'danger');
        if (resultEl) resultEl.innerHTML = '<div class="alert alert-danger">Memory import failed: ' + escapeHtml(err.message || err) + '</div>';
      } finally {
        if (importBtn) {
          importBtn.disabled = false;
          importBtn.textContent = originalLabel || 'Import';
        }
      }
    }
    function displayRecords(records) {
      const tbody = document.getElementById('mem-results-body');
      if (!records.length) { tbody.innerHTML = '<tr><td colspan=5 class=empty>No records found.</td></tr>'; return; }
      tbody.innerHTML = records.map(r => `
        <tr>
          <td><span class="badge badge-${escapeHtml(r.memory_class)}">${escapeHtml(r.memory_class)}</span></td>
          <td>${escapeHtml((r.content || '').substring(0, 80))}</td>
          <td><code>${escapeHtml((r.scope || '').replace('workspace:', ''))}</code></td>
          <td>${(r.confidence || 0.5).toFixed(1)}</td>
          <td><button class="btn btn-sm btn-danger" data-record-id="${escapeHtml(r.id)}" onclick="retractRecord(this.dataset.recordId)">Retract</button></td>
        </tr>`).join('');
    }
    function copyGeneratedOutput(btn) {
      copyToClipboard(document.querySelector('#ig-output pre').textContent, btn);
    }

    document.addEventListener('click', function(ev) {
      const decide = ev.target.closest('[data-proposal-verdict]');
      if (decide) {
        ev.preventDefault();
        decideProposal(
          decide.dataset.proposalId,
          decide.dataset.proposalVerdict,
          decide.dataset.proposalOutcome
        );
        return;
      }
      const retract = ev.target.closest('[data-memory-retract]');
      if (retract) { ev.preventDefault(); retractRecord(retract.dataset.memoryRetract); return; }
      const del = ev.target.closest('[data-memory-delete]');
      if (del) { ev.preventDefault(); deleteRecord(del.dataset.memoryDelete); return; }
      const detail = ev.target.closest('[data-memory-detail]');
      if (detail) { ev.preventDefault(); viewMemory(detail.dataset.memoryDetail); return; }
      const restore = ev.target.closest('[data-memory-restore]');
      if (restore) { ev.preventDefault(); restoreRecord(restore.dataset.memoryRestore); return; }
      const reanchor = ev.target.closest('[data-proposal-reanchor]');
      if (reanchor) { ev.preventDefault(); reanchorProposal(reanchor.dataset.proposalReanchor); }
    });

    async function generateProposals() {
      const j = await apiFetch('/api/memory/proposals/generate', { method: 'POST', body: '{}' });
      if (!j.ok) { showToast(j.error?.message || 'Review failed', 'danger'); return; }
      const created = j.data.created;
      if (created > 0) { showToast(created + ' memor' + (created === 1 ? 'y' : 'ies') + ' to look at'); refreshMemory(); }
      // A pass that finds nothing is the expected steady state, not a failure.
      else { showToast('Nothing new to look at', 'info'); }
    }
    async function reanchorProposal(id) {
      const anchor = document.getElementById('anchor-' + id).value.trim();
      const j = await apiFetch('/api/memory/proposals/' + id + '/reanchor', {
        method: 'POST', body: JSON.stringify({ subject_anchor: anchor })
      });
      if (!j.ok) { showToast(j.error?.message || 'Failed', 'danger'); return; }
      // Say whether the new pointer actually resolves, rather than just "saved".
      const recheck = (j.data.proposal.recheck || [])[0];
      if (recheck && recheck.status === 'verified') showToast('Pointer fixed and checked — the memory is confirmed');
      else if (recheck && recheck.status === 'missing') showToast('Saved, but that location is not there either', 'warning');
      else showToast('Pointer fixed; it could not be checked from here');
      refreshMemory();
    }
    async function decideProposal(id, verdict, outcome) {
      const j = await apiFetch('/api/memory/proposals/' + id + '/decide', {
        method: 'POST', body: JSON.stringify({ verdict: verdict, outcome: outcome || null })
      });
      if (!j.ok) { showToast(j.error?.message || 'Failed', 'danger'); return; }
      // Say what happened to the memory, not what happened to the suggestion.
      if (verdict !== 'accepted') { showToast('Left alone — it will not be flagged again'); }
      else if (outcome === 'still_current') { showToast('Marked as checked today'); }
      else { showToast('Retracted — find it under Retracted Records to restore'); }
      refreshMemory();
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
        <button class="btn btn-secondary" onclick="openModal('import-memory-modal')">Import Notes</button>
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
      <table><thead><tr><th>Class</th><th>Content</th><th>Scope</th><th>Confidence</th><th class="actions-cell">Actions</th></tr></thead>
      <tbody id="mem-results-body">
        {records_rows}
      </tbody>
      <input type="hidden" id="current-scope" value="{user_scope}">
    </div>

    {review_html}

    <!-- Retracted Records -->
    """
            + (
                f"""
    <div class="card" style="border-left:4px solid var(--text-muted)">
      <h3 style="color:var(--text-muted)">Retracted Records <span class="text-muted" style="font-weight:normal;font-size:0.8rem">({len(retracted_records)})</span></h3>
      <p class="text-muted" style="font-size:0.85rem;margin-bottom:8px">These records are hidden from search. Restore to make them active again, or permanently delete.</p>
      <table><thead><tr><th>Class</th><th>Content</th><th>Scope</th><th>Confidence</th><th class="actions-cell">Actions</th></tr></thead>
      <tbody>{retracted_rows or "<tr><td colspan=5 class=empty>No retracted records.</td></tr>"}</tbody></table>
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
                <p class="form-hint">Optional tag for searches and filtering.</p>
              </div>
              <div class="form-group">
                <label>Topic</label>
                <input type="text" id="mem-topic" placeholder="e.g. style" autocomplete="off">
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
                <input type="text" id="mem-slot-key" placeholder="e.g. style" autocomplete="off">
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

    <!-- Import Memory Modal -->
    <div class="modal-overlay" id="import-memory-modal" style="display:none">
      <div class="modal" style="max-width:680px">
        <h3>Import Notes</h3>
        <form id="import-memory-form" onsubmit="doImport(event)">
          <div class="form-group">
            <label>Text files</label>
            <input type="file" id="mem-import-files" multiple accept=".txt,.md,.markdown,text/plain,text/markdown">
            <p class="form-hint">Import curated handoffs, decision notes, project facts, or markdown summaries into normal memory records. If a file is mostly agent instructions, extract only the durable facts first.</p>
            <p class="form-hint">For raw instruction files like `CLAUDE.md` or `AGENTS.md`, AI-assisted extract-and-review is usually the better fit; this import path is for the distilled notes.</p>
            <details style="margin-top:8px">
              <summary class="text-muted" style="cursor:pointer">What good notes look like</summary>
              <div style="margin-top:8px">
                <p class="form-hint" style="margin-bottom:8px">Use short, durable statements that a future agent could search and reuse. One idea per line or paragraph works best.</p>
                <pre style="white-space:pre-wrap;background:var(--bg);padding:10px;border-radius:6px;margin:0;overflow:auto"># Project Notes
- Workspace: sage
- Decision: default imported notes to `fact`
- Fact: keep the import literal, not AI-summarized
- Preference: use concise notes with explicit scope and topic</pre>
              </div>
            </details>
          </div>
          <div class="form-group">
            <label>Pasted notes</label>
            <input type="text" id="mem-import-name" placeholder="pasted-notes.txt" autocomplete="off" style="margin-bottom:6px">
            <textarea id="mem-import-text" rows="7" placeholder="Paste curated notes here when you are not importing a file"></textarea>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Save as</label>
              <select id="mem-import-class">
                <option value="fact">Fact</option>
                <option value="decision">Decision</option>
                <option value="scratchpad">Scratchpad</option>
              </select>
            </div>
            <div class="form-group">
              <label>Save to</label>
              <select id="mem-import-scope" required>
                <option value="{user_scope}" selected>{user_scope_label}</option>
                {agent_options}
                {project_options}
                <option value="shared">Shared memory (shared)</option>
              </select>
            </div>
          </div>
          <details style="margin-top:4px">
            <summary class="text-muted" style="cursor:pointer">More options</summary>
            <div class="form-row" style="margin-top:10px">
            <div class="form-group">
            </div>
            <div class="form-group">
              <label>Topic</label>
              <input type="text" id="mem-import-topic" placeholder="Defaults to source filename" autocomplete="off">
            </div>
          </div>
          <div class="form-row">
            <div class="form-group">
              <label>Confidence</label>
              <input type="number" id="mem-import-confidence" value="0.85" min="0" max="1" step="0.05">
              <p class="form-hint">Fact is the default save class for imports. Switch to Decision only when the note records a chosen direction.</p>
            </div>
            <div class="form-group">
              <label>Importance</label>
              <input type="number" id="mem-import-importance" value="0.6" min="0" max="1" step="0.05">
            </div>
            </div>
          </details>
          <div id="mem-import-result" style="margin-top:10px"></div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('import-memory-modal')">Cancel</button>
            <button type="submit" class="btn" id="mem-import-submit" onclick="doImport(event)">Import</button>
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
        <div>
          <h4 style="margin-top:12px">Version History</h4>
          <p class="text-muted" style="margin:4px 0 10px;font-size:0.85rem">Current record and any earlier versions it replaced.</p>
          <div id="mem-chain-content" style="font-size:0.85rem;max-height:220px;overflow:auto"></div>
        </div>
        <div class="modal-footer">
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


