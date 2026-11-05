"""Approvals dashboard page: what an agent proposed, and what a person decides.

An agent with the admin_propose capability proposes admin changes
(app.services.approval_service); a signed-in admin decides them here. This
page never runs an operation itself: the buttons post to the decide endpoint,
which runs the operations as the person who pressed them.

Everything a person or an agent can type into a proposal reaches this page,
so every dynamic value is escaped and ids travel as data attributes rather
than as inline handler arguments.
"""

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, Request

from app.routes.dashboard_shared import escape_html, local_dt, render_page, require_auth

router = APIRouter()

# The approver sees every parameter that will be applied. Hiding one by its
# name would let a proposal carry a value the person cannot check. Only a
# person field (typed by the approver, never proposed) is shown as a blank.
REDACTED = "[hidden]"

# Statuses that are no longer waiting on a person.
DECIDED_STATUSES = ("executing", "done", "failed", "rejected", "expired")

PENDING_LIMIT = 200
DECIDED_LIMIT = 20

_STATUS_BADGES = {
    "pending": "badge-warning",
    "executing": "badge-info",
    "done": "badge-success",
    "failed": "badge-danger",
    "rejected": "badge-secondary",
    "expired": "badge-stale",
}

_EMPTY_PARAMS_ROW = "<tr><td colspan='2' class='empty'>No parameters.</td></tr>"


def _approvals_session(request: Request) -> dict:
    """The person on an Approvals page.

    A device the person marked as trusted holds a session that only the
    Approvals routes accept (see auth_service.approvals_session), so approving
    from a phone needs no sign-in. Everyone else needs the ordinary dashboard
    session or an operator token, which require_auth handles.
    """
    from app.services.auth_service import approvals_session

    session = approvals_session(request)
    if session:
        return session
    return require_auth(request)


def _read_scopes(session: dict) -> Optional[list[str]]:
    """The scopes whose approvals this person may see. None means every scope."""
    if session.get("role") == "admin":
        return None
    from app.security.context import build_user_context

    return list(build_user_context(session).read_scopes)


def _may_read(session: dict, scope: str) -> bool:
    """Whether this person may open an approval proposed in `scope`."""
    if session.get("role") == "admin":
        return True
    from app.security.context import build_user_context
    from app.security.scope_enforcer import ScopeEnforcer

    ctx = build_user_context(session)
    enforcer = ScopeEnforcer(
        ctx.read_scopes, ctx.write_scopes, ctx.agent_id,
        is_admin=False, active_workspace_ids=ctx.active_workspace_ids,
    )
    return enforcer.can_read(scope or "")


def _status_badge(status: str) -> str:
    css = _STATUS_BADGES.get(status, "badge-secondary")
    return "<span class='badge " + css + "'>" + escape_html(status) + "</span>"


def _param_text(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, sort_keys=True, default=str)
    if value is None:
        return ""
    return str(value)


def _row(approval: dict) -> str:
    approval_id = escape_html(approval.get("id") or "")
    title = escape_html(approval.get("title") or "")
    scope = escape_html(approval.get("scope") or "")
    proposed_by = escape_html(approval.get("proposed_by") or "")
    status_badge = _status_badge(approval.get("status") or "")
    created = local_dt(approval.get("created_at"))
    return (
        "<tr>"
        "<td><a href='/approvals/" + approval_id + "'>" + title + "</a></td>"
        "<td><code>" + scope + "</code></td>"
        "<td>" + proposed_by + "</td>"
        "<td>" + status_badge + "</td>"
        "<td>" + created + "</td>"
        "</tr>"
    )


def _list_rows(approvals: list[dict]) -> str:
    return "".join(_row(a) for a in approvals)


def _operation_card(index: int, operation: dict, can_decide: bool) -> str:
    """One proposed operation: what it is, and what it would change."""
    from app.operations import admin_registry

    name = operation.get("name") or ""
    definition = admin_registry.get(name)
    summary = definition.summary if definition else ""
    person_fields = definition.person_fields if definition else ()
    params = operation.get("params") or {}

    param_rows = ""
    for key, value in params.items():
        shown = REDACTED if str(key) in person_fields else _param_text(value)
        param_rows += (
            "<tr>"
            "<td><code>" + escape_html(str(key)) + "</code></td>"
            "<td>" + escape_html(shown) + "</td>"
            "</tr>"
        )
    if not param_rows:
        param_rows = _EMPTY_PARAMS_ROW

    person_inputs = ""
    if can_decide:
        for field_name in person_fields:
            field_id = escape_html(f"person-{index}-{field_name}")
            # The label counts operations the way a person does, from 1. The
            # value key counts them the way the decide endpoint does, from 0.
            values_key = escape_html(f"{index}.{field_name}")
            label = escape_html(field_name) + f" for operation {index + 1}"
            person_inputs += (
                "<div class='form-group'>"
                "<label for='" + field_id + "'>" + label + "</label>"
                "<input type='text' id='" + field_id + "'"
                " data-person-field='" + values_key + "' autocomplete='off'>"
                "<p class='form-hint'>The agent did not propose this value; you supply it now.</p>"
                "</div>"
            )

    heading = f"Operation {index + 1}: " + escape_html(name)
    summary_html = "<p class='text-muted' style='font-size:0.85rem'>" + escape_html(summary) + "</p>"
    return (
        "<div class='card'>"
        "<h3>" + heading + "</h3>"
        + summary_html
        + "<table><thead><tr><th>Parameter</th><th>Value</th></tr></thead>"
        "<tbody>" + param_rows + "</tbody></table>"
        + person_inputs
        + "</div>"
    )


def _decision_card(view: dict) -> str:
    """What the person decided, for anyone who did not decide it themselves."""
    decided_by = escape_html(view.get("decided_by") or "") or "—"
    decision_note = escape_html(view.get("decision_note") or "") or "—"
    decided_at = local_dt(view.get("decided_at"))
    error = view.get("error") or ""
    result = view.get("result")

    error_html = ""
    if error:
        error_html = (
            "<tr><td>Error</td><td>"
            "<span class='badge badge-danger'>failed</span> " + escape_html(error) +
            "</td></tr>"
        )
    result_html = ""
    if result:
        result_text = escape_html(json.dumps(result, indent=2, default=str))
        result_html = (
            "<tr><td>Result</td><td>"
            "<pre style='background:var(--bg-secondary);padding:0.75rem;border-radius:4px;"
            "overflow-x:auto;white-space:pre-wrap'>" + result_text + "</pre></td></tr>"
        )
    return (
        "<div class='card'>"
        "<h3>Decision</h3>"
        "<table><tbody>"
        "<tr><td>Decided by</td><td>" + decided_by + "</td></tr>"
        "<tr><td>Decided at</td><td>" + decided_at + "</td></tr>"
        "<tr><td>Note</td><td>" + decision_note + "</td></tr>"
        + error_html
        + result_html
        + "</tbody></table>"
        "</div>"
    )


def _decide_card(approval_id: str) -> str:
    """The buttons, and the note the person may leave with their decision."""
    safe_id = escape_html(approval_id)
    return (
        "<div class='card'>"
        "<h3>Your decision</h3>"
        "<p class='text-muted' style='font-size:0.85rem'>"
        "Approving runs every operation above, together, as you. Rejecting leaves "
        "nothing changed."
        "</p>"
        "<div class='form-group'>"
        "<label for='approval-note'>Note (optional)</label>"
        "<textarea id='approval-note' rows='3' placeholder='Why you decided this way'></textarea>"
        "</div>"
        "<div class='actions-cell'>"
        "<button type='button' class='btn' data-approval-approve='" + safe_id + "'>Approve</button>"
        "<button type='button' class='btn btn-danger' data-approval-reject='" + safe_id + "'>Reject</button>"
        "</div>"
        "</div>"
    )


def _not_found_page(session: dict):
    return render_page(
        "Approval Not Found",
        """
<div class="page-header"><h1>Approval Not Found</h1></div>
<div class="card"><p>There is no approval with that id, or it is not one you may read.</p>
<a href="/approvals" class="btn">Back to Approvals</a></div>""",
        nav_active="/approvals",
        session=session,
        status_code=404,
    )


@router.get("/approvals")
def approvals_page(request: Request, session: dict = Depends(_approvals_session)):
    from app.services import approval_service

    scopes = _read_scopes(session)
    pending = approval_service.list_for_scopes(scopes, "pending", PENDING_LIMIT)
    decided: list[dict] = []
    for status in DECIDED_STATUSES:
        decided.extend(approval_service.list_for_scopes(scopes, status, DECIDED_LIMIT))
    decided.sort(key=lambda a: a.get("created_at") or "", reverse=True)
    decided = decided[:DECIDED_LIMIT]

    pending_rows = _list_rows(pending)
    decided_rows = _list_rows(decided)

    body = """
<div class="page-header"><h1>Approvals</h1></div>
<div class="card">
  <h3>Waiting for a decision</h3>
  <p class="text-muted" style="font-size:0.85rem">
    An agent proposed an admin change. Nothing runs until a person decides it.
  </p>
  <div style="overflow-x:auto"><table>
    <thead><tr><th>Title</th><th>Scope</th><th>Proposed by</th><th>Status</th><th>Created</th></tr></thead>
    <tbody>""" + (pending_rows or "<tr><td colspan='5' class='empty'>Nothing is waiting on you.</td></tr>") + """</tbody>
  </table></div>
</div>
<div class="card">
  <h3>Recently decided</h3>
  <div style="overflow-x:auto"><table>
    <thead><tr><th>Title</th><th>Scope</th><th>Proposed by</th><th>Status</th><th>Created</th></tr></thead>
    <tbody>""" + (decided_rows or "<tr><td colspan='5' class='empty'>Nothing has been decided yet.</td></tr>") + """</tbody>
  </table></div>
</div>
"""

    return render_page("Approvals", body, nav_active="/approvals", session=session)


@router.get("/approvals/{approval_id}")
def approval_detail_page(
    approval_id: str, request: Request, session: dict = Depends(_approvals_session)
):
    from app.services import approval_service

    row = approval_service.get(approval_id)
    if row is None or not _may_read(session, row.get("scope") or ""):
        return _not_found_page(session)

    view = approval_service.public_view(row, include_operations=True)
    is_admin = session.get("role") == "admin"
    can_decide = is_admin and view.get("status") == "pending"

    operation_cards = "".join(
        _operation_card(index, operation, can_decide)
        for index, operation in enumerate(view.get("operation_details") or [])
    )

    title = escape_html(view.get("title") or "Approval")
    scope = escape_html(view.get("scope") or "")
    proposed_by = escape_html(view.get("proposed_by") or "")
    note = escape_html(view.get("note") or "") or "—"
    status_badge = _status_badge(view.get("status") or "")
    created = local_dt(view.get("created_at"))
    expires = local_dt(view.get("expires_at"))

    decide_card = _decide_card(approval_id) if can_decide else ""
    decision_card = "" if can_decide else _decision_card(view)

    body = """
<div class="page-header"><h1>""" + title + """</h1></div>
<div class="card">
  <table><tbody>
    <tr><td style="width:180px">Status</td><td>""" + status_badge + """</td></tr>
    <tr><td>Scope</td><td><code>""" + scope + """</code></td></tr>
    <tr><td>Proposed by</td><td>""" + proposed_by + """</td></tr>
    <tr><td>Created</td><td>""" + created + """</td></tr>
    <tr><td>Expires</td><td>""" + expires + """</td></tr>
    <tr><td>Note</td><td style="white-space:pre-wrap">""" + note + """</td></tr>
  </tbody></table>
</div>
""" + operation_cards + decide_card + decision_card

    js = """
<script>
document.addEventListener('click', function(ev) {
  const approve = ev.target.closest('[data-approval-approve]');
  if (approve) { ev.preventDefault(); decideApproval(approve.dataset.approvalApprove, true); return; }
  const reject = ev.target.closest('[data-approval-reject]');
  if (reject) { ev.preventDefault(); decideApproval(reject.dataset.approvalReject, false); }
});

// The approval id travels as data, never as handler source: entities in an
// inline handler are decoded before the JavaScript is parsed, so an escaped
// quote there turns back into executable source.
async function decideApproval(id, approve) {
  const values = {};
  document.querySelectorAll('[data-person-field]').forEach(function(el) {
    const typed = (el.value || '').trim();
    if (typed) values[el.dataset.personField] = typed;
  });
  const noteEl = document.getElementById('approval-note');
  const note = noteEl ? (noteEl.value || '').trim() : '';
  const j = await apiFetch('/api/approvals/' + encodeURIComponent(id) + '/decide', {
    method: 'POST',
    body: JSON.stringify({approve: approve, note: note || null, values: values})
  });
  if (j.ok) { location.reload(); return; }
  showToast((j.error && j.error.message) || 'The decision did not go through', 'danger');
}
</script>
"""

    return render_page(
        title,
        body,
        nav_active="/approvals",
        extra_js=js,
        session=session,
    )
