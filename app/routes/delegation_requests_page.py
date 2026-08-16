"""Delegation-request dashboard page for human operators."""

import json

from fastapi import APIRouter, Depends, Request

from app.routes.dashboard_shared import escape_html, local_dt, render_page, require_auth
from app.security.context import build_user_context
from app.security.effective_authority import permanent_authority


router = APIRouter()


def _permission_summary(request: dict) -> str:
    items = []
    for permission in request.get("scope_permissions", []):
        items.append(
            f"{permission['resource_type']}:{permission['operation']} "
            f"in {permission['scope']}"
        )
    for permission in request.get("resource_permissions", []):
        items.append(
            f"{permission['resource_type']}:{permission['operation']} "
            f"{permission['resource_id']}"
        )
    for permission in request.get("binding_actions", []):
        items.append(f"binding {permission['binding_id']} → {permission['action']}")
    if not items:
        return "<span class='text-muted'>No permissions</span>"
    entries = "".join(f"<li><code>{escape_html(item)}</code></li>" for item in items)
    return f"<details><summary>{len(items)} requested permission{'s' if len(items) != 1 else ''}</summary><ul style='margin:8px 0 0;padding-left:20px'>{entries}</ul></details>"


@router.get("/delegation-requests")
async def delegation_requests_page(request: Request, session: dict = Depends(require_auth)):
    from app.services import delegation_service

    authority = permanent_authority(build_user_context(session))
    requests = delegation_service.list_requests(authority)
    pending = [item for item in requests if item.get("status") == "pending"]
    grants_by_id = {
        grant["id"]: grant for grant in delegation_service.list_grants(authority)
    }

    rows = ""
    for item in requests:
        status = item.get("status", "unknown")
        status_class = {
            "pending": "stale",
            "approved": "active",
            "denied": "cancelled",
        }.get(status, "secondary")
        action_html = "<span class='text-muted'>Decided</span>"
        if status == "pending":
            action_html = (
                "<div class='actions-cell'>"
                f"<button class='btn btn-sm' data-delegation-approve='{escape_html(item['id'])}'>Review &amp; approve</button> "
                f"<button class='btn btn-sm btn-danger' data-delegation-deny='{escape_html(item['id'])}'>Deny</button>"
                "</div>"
            )
        decided_by = item.get("decided_by_actor_id") or "—"
        grant_id = item.get("grant_id") or "—"
        grant = grants_by_id.get(item.get("grant_id"))
        decision_detail = ""
        if item.get("decided_at"):
            decision_detail += f"<br>{local_dt(item['decided_at'])}"
        if item.get("decision_reason"):
            decision_detail += f"<br><span class='text-muted'>{escape_html(item['decision_reason'])}</span>"
        grant_detail = ""
        if grant:
            grant_detail = (
                f"<br><code>{escape_html(grant_id)}</code>"
                f"<br><span class='text-muted'>{escape_html(grant.get('status'))}</span>"
            )
            if grant.get("claim_expires_at"):
                grant_detail += f"<br><span class='text-muted'>Claim by {local_dt(grant['claim_expires_at'])}</span>"
        rows += (
            "<tr>"
            f"<td><code>{escape_html(item['id'])}</code><br>{local_dt(item.get('created_at'))}</td>"
            f"<td><code>{escape_html(item.get('requester_actor_type'))}:{escape_html(item.get('requester_actor_id'))}</code><br>"
            f"to <code>{escape_html(item.get('recipient_agent_id'))}</code></td>"
            f"<td>{escape_html(item.get('purpose'))}<br><span class='text-muted'>{escape_html(item.get('target_user_id'))}</span></td>"
            f"<td>{escape_html(item.get('ttl_seconds'))} seconds</td>"
            f"<td>{_permission_summary(item)}</td>"
            f"<td><span class='badge badge-{status_class}'>{escape_html(status)}</span><br>"
            f"<span class='text-muted'>{escape_html(decided_by)}</span>{decision_detail}{grant_detail}</td>"
            f"<td>{action_html}</td>"
            "</tr>"
        )
    if not rows:
        rows = "<tr><td colspan='7' class='text-muted' style='text-align:center'>No delegation requests are visible to this account.</td></tr>"

    request_data = json.dumps({item["id"]: item for item in pending}).replace("<", "\\u003c")
    body = f"""
<div class='page-header'>
  <div><h1>Delegation Requests</h1><p class='text-muted'>Review explicit, short-lived authority requests. Approval can only keep or narrow the requested permissions.</p></div>
  <span class='badge badge-stale'>{len(pending)} pending</span>
</div>
<div class='card' style='margin-bottom:1rem'>
  <p style='margin:0'>Only a logged-in human can approve here. Approval uses your current authority, creates no reusable secret, and records the decision in audit history.</p>
</div>
<div class='card'>
  <div style='overflow-x:auto'><table class='data-table'>
    <thead><tr><th>Request</th><th>Requester / recipient</th><th>Purpose / principal</th><th>TTL</th><th>Permissions</th><th>Status</th><th>Actions</th></tr></thead>
    <tbody>{rows}</tbody>
  </table></div>
</div>

<div class='modal-overlay' id='delegation-approve-modal' style='display:none'>
  <div class='modal' style='max-width:760px'>
    <h3>Approve delegation request</h3>
    <p id='delegation-approve-summary' class='text-muted'></p>
    <p>Keep the permissions you approve. Removing a permission narrows the grant; adding authority is not possible.</p>
    <div id='delegation-permissions'></div>
    <div id='delegation-approve-error' class='error-box' style='display:none'></div>
    <div class='modal-actions'>
      <button class='btn' id='delegation-approve-submit'>Approve selected permissions</button>
      <button class='btn btn-secondary' data-delegation-close='delegation-approve-modal'>Cancel</button>
    </div>
  </div>
</div>

<div class='modal-overlay' id='delegation-deny-modal' style='display:none'>
  <div class='modal' style='max-width:560px'>
    <h3>Deny delegation request</h3>
    <p id='delegation-deny-summary' class='text-muted'></p>
    <label>Reason <span class='text-muted'>(optional)</span><textarea id='delegation-deny-reason' rows='3' placeholder='Why this authority is not approved'></textarea></label>
    <div id='delegation-deny-error' class='error-box' style='display:none'></div>
    <div class='modal-actions'>
      <button class='btn btn-danger' id='delegation-deny-submit'>Deny request</button>
      <button class='btn btn-secondary' data-delegation-close='delegation-deny-modal'>Cancel</button>
    </div>
  </div>
</div>
"""

    js = f"""
<script>
const delegationRequests = {request_data};
let selectedDelegationRequest = null;

function delegationEscape(value) {{
  const node = document.createElement('span');
  node.textContent = String(value ?? '');
  return node.innerHTML;
}}

function openDelegationModal(id) {{ document.getElementById(id).style.display = 'flex'; }}
function closeDelegationModal(id) {{ document.getElementById(id).style.display = 'none'; }}
function delegationError(id, message) {{
  const box = document.getElementById(id);
  box.textContent = message || '';
  box.style.display = message ? 'block' : 'none';
}}
function permissionLabel(kind, permission) {{
  if (kind === 'scope_permissions') return permission.resource_type + ':' + permission.operation + ' in ' + permission.scope;
  if (kind === 'resource_permissions') return permission.resource_type + ':' + permission.operation + ' ' + permission.resource_id;
  return 'binding ' + permission.binding_id + ' → ' + permission.action;
}}
function openDelegationApproval(id) {{
  const item = delegationRequests[id];
  if (!item) return;
  selectedDelegationRequest = item;
  document.getElementById('delegation-approve-summary').textContent = item.purpose + ' — ' + item.requester_actor_type + ':' + item.requester_actor_id + ' to ' + item.recipient_agent_id;
  delegationError('delegation-approve-error', '');
  const groups = ['scope_permissions', 'resource_permissions', 'binding_actions'];
  let html = '';
  groups.forEach((kind) => (item[kind] || []).forEach((permission, index) => {{
    html += '<label class="checkbox-label" style="margin:8px 0"><input type="checkbox" checked data-delegation-kind="' + kind + '" data-delegation-index="' + index + '"> <code>' + delegationEscape(permissionLabel(kind, permission)) + '</code></label>';
  }}));
  document.getElementById('delegation-permissions').innerHTML = html || '<p class="text-muted">No permissions requested.</p>';
  openDelegationModal('delegation-approve-modal');
}}
async function approveDelegationRequest() {{
  const item = selectedDelegationRequest;
  if (!item) return;
  const body = {{scope_permissions: [], resource_permissions: [], binding_actions: []}};
  document.querySelectorAll('#delegation-permissions input:checked').forEach((input) => {{
    body[input.dataset.delegationKind].push(item[input.dataset.delegationKind][Number(input.dataset.delegationIndex)]);
  }});
  if (!body.scope_permissions.length && !body.resource_permissions.length && !body.binding_actions.length) {{
    delegationError('delegation-approve-error', 'Keep at least one permission to approve this request.');
    return;
  }}
  const result = await apiFetch('/api/delegation-requests/' + encodeURIComponent(item.id) + '/approve', {{method: 'POST', body: JSON.stringify(body)}});
  if (!result.ok) {{ delegationError('delegation-approve-error', result.error?.message || 'Approval failed.'); return; }}
  closeDelegationModal('delegation-approve-modal');
  showToast('Delegation request approved', 'success');
  window.location.reload();
}}
function openDelegationDenial(id) {{
  const item = delegationRequests[id];
  if (!item) return;
  selectedDelegationRequest = item;
  document.getElementById('delegation-deny-summary').textContent = item.purpose + ' — ' + item.requester_actor_type + ':' + item.requester_actor_id + ' to ' + item.recipient_agent_id;
  document.getElementById('delegation-deny-reason').value = '';
  delegationError('delegation-deny-error', '');
  openDelegationModal('delegation-deny-modal');
}}
async function denyDelegationRequest() {{
  const item = selectedDelegationRequest;
  if (!item) return;
  const reason = document.getElementById('delegation-deny-reason').value.trim();
  const result = await apiFetch('/api/delegation-requests/' + encodeURIComponent(item.id) + '/deny', {{method: 'POST', body: JSON.stringify({{reason: reason || null}})}});
  if (!result.ok) {{ delegationError('delegation-deny-error', result.error?.message || 'Denial failed.'); return; }}
  closeDelegationModal('delegation-deny-modal');
  showToast('Delegation request denied', 'success');
  window.location.reload();
}}
document.addEventListener('click', (event) => {{
  const approve = event.target.closest('[data-delegation-approve]');
  if (approve) {{ openDelegationApproval(approve.dataset.delegationApprove); return; }}
  const deny = event.target.closest('[data-delegation-deny]');
  if (deny) {{ openDelegationDenial(deny.dataset.delegationDeny); return; }}
  const close = event.target.closest('[data-delegation-close]');
  if (close) closeDelegationModal(close.dataset.delegationClose);
}});
document.getElementById('delegation-approve-submit').addEventListener('click', approveDelegationRequest);
document.getElementById('delegation-deny-submit').addEventListener('click', denyDelegationRequest);
</script>
"""
    return render_page(
        "Delegation Requests", body, nav_active="/delegation-requests", extra_js=js, session=session
    )
