"""Webhooks dashboard page. Split from dashboard.py — see
private/dashboard-split-plan.md."""

from fastapi import APIRouter, Request, Depends

from app.branding import APP_NAME
from app.routes.dashboard_shared import render_page, require_auth, escape_html, get_icon

router = APIRouter()


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
