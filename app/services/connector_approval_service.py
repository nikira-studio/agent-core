"""Service actions that wait for a person before they run.

A gated action (see action_gate_service) is not executed when an agent, a token
or a script asks for it. The exact request is stored encrypted in the approvals
table as a ``connector_run`` approval; a signed-in person approves or rejects it
on the Approvals page. Approval re-checks that the binding is unchanged and the
caller still has access, then runs the request once, as the caller who asked,
and stores the result for the caller to read.

Nothing here lets a caller decide, release or edit a request: the decide
endpoint is cookie-only, and a gate is changed only by a person in the dashboard.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from datetime import timedelta
from typing import Any, Optional

from app.config import settings
from app.database import get_db
from app.security.encryption import decrypt_value, encrypt_value
from app.security.exceptions import APIError
from app.services import approval_service
from app.time_utils import utc_now, utc_now_iso

KIND = "connector_run"
OPERATION = "connector.run"
# The result is stored for the caller to read, so it is capped like any other
# stored value; a larger answer keeps its start and says it was cut.
MAX_RESULT_CHARS = 200_000


def _actor_of(authority) -> dict:
    from app.security.effective_authority import SystemAuthority

    if isinstance(authority, SystemAuthority):
        return {"type": "system", "id": "system", "reason": authority.reason}
    return {"type": authority.actor_type, "id": authority.actor_id, "user_id": authority.user_id}


def _binding_fingerprint(binding: dict, connector_type: dict) -> str:
    """What the request was about: change any of it and the request must be asked for again."""
    parts = {
        "binding": binding["id"],
        "type": binding["connector_type_id"],
        "scope": binding["scope"],
        "credential_id": binding.get("credential_id"),
        "config": binding.get("config_json"),
        "endpoint_override": binding.get("endpoint_url_override"),
        "type_endpoint": connector_type.get("endpoint_url"),
        "type_backend": connector_type.get("backend_json"),
    }
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def plain_sentence(row: dict, payload: dict, binding: Optional[dict], connector_type: Optional[dict]) -> str:
    actor = payload["actor"]
    who = actor["id"] if actor["type"] != "system" else "Agent Core itself"
    if actor["type"] == "user":
        who = f"{actor['id']} (using a token or script)"
    service = (connector_type or {}).get("display_name") or payload["binding_id"]
    where = (binding or {}).get("scope", row["scope"]).replace("workspace:", "workspace ")
    return f"{who} wants to run {payload['action']} on the {service} service (binding {(binding or {}).get('name', payload['binding_id'])}, {where})."


def hold(binding: dict, connector_type: dict, action: str, params: Optional[dict], authority) -> dict:
    """Store this request for a person and return what the caller is told instead of a result."""
    actor = _actor_of(authority)
    payload = {"binding_id": binding["id"], "action": action, "params": params or {}, "actor": actor}
    operations = [{"name": OPERATION, "params": payload}]
    request_hash = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    proposer = actor["id"]
    now = utc_now()
    expiry_hours = max(1, min(int(settings.CONNECTOR_APPROVAL_EXPIRY_HOURS), 168))
    with get_db() as conn:
        approval_service._sweep(conn)
        existing = conn.execute(
            "SELECT * FROM approvals WHERE request_hash = ? AND proposed_by = ? AND status = 'pending' AND kind = ?",
            (request_hash, proposer, KIND),
        ).fetchone()
        if existing:
            return pending_notice(dict(existing))
        mine = conn.execute(
            "SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND proposed_by = ?", (proposer,)
        ).fetchone()[0]
        if mine >= approval_service.MAX_PENDING_PER_AGENT:
            raise APIError(
                "TOO_MANY_PENDING",
                f"{mine} requests are already waiting for a person; wait for a decision before asking for more", 429,
            )
        total = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
        if total >= approval_service.MAX_PENDING_TOTAL:
            raise APIError("TOO_MANY_PENDING", "Too many requests are waiting for a person; try again later", 429)
        record = {
            "id": uuid.uuid4().hex,
            "status": "pending",
            "title": f"Run {action} on {connector_type.get('display_name') or connector_type['id']}"[:200],
            "note": None,
            "scope": binding["scope"],
            "proposed_by": proposer,
            "proposed_execution_id": None,
            "operations_enc": encrypt_value(json.dumps(operations, default=str)),
            "fingerprints_json": json.dumps([_binding_fingerprint(binding, connector_type)]),
            "request_hash": request_hash,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=expiry_hours)).isoformat(),
            "kind": KIND,
        }
        conn.execute(
            f"INSERT INTO approvals ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})",
            tuple(record.values()),
        )
        from app.services.workspace_sync_service import record_change

        record_change(
            conn, memory_scope=binding["scope"], change_type="approval_requested",
            resource_type="approval", resource_id=record["id"],
            summary={"title": record["title"], "kind": KIND},
            source_agent_id=actor["id"] if actor["type"] == "agent" else None,
        )
        conn.commit()
    approval_service._announce(record, operations, actor_type=actor["type"])
    return pending_notice(record)


def pending_notice(row: dict) -> dict:
    """What the caller gets back: not an error, so it waits instead of retrying."""
    return {
        "success": False,
        "pending_approval": True,
        "approval_id": row["id"],
        "status": "pending",
        "error_code": "PENDING_APPROVAL",
        "message": (
            "A person must approve this before it runs. Do not retry. "
            "Check on it with approval_get using this approval id."
        ),
        "expires_at": row["expires_at"],
        "path": f"/approvals/{row['id']}",
    }


def payload_of(row: dict) -> dict:
    return json.loads(decrypt_value(row["operations_enc"]))[0]["params"]


def can_decide(session: dict, scope: str) -> bool:
    """An admin, or someone who can write in the workspace the binding belongs to."""
    if session.get("role") == "admin":
        return True
    if scope.startswith("workspace:"):
        from app.services import workspace_service

        return bool(workspace_service.can_user_write_workspace(session["user_id"], scope.split(":", 1)[1]))
    return scope == f"user:{session['user_id']}"


def decide(row: dict, *, session: dict, approve: bool, note: Optional[str]) -> dict:
    """Approve (and start the run) or reject a held request, as the person in `session`."""
    from app.services import audit_service

    approval_id = row["id"]
    if not can_decide(session, row["scope"]):
        raise APIError("FORBIDDEN", "You need write access to this workspace to decide this request", 403)
    note = (note or "").strip()[:2000] or None
    with get_db() as conn:
        approval_service._sweep(conn)
        claimed = conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
            "WHERE id = ? AND status = 'pending' AND kind = ?",
            ("executing" if approve else "rejected", session["user_id"], utc_now_iso(), note, approval_id, KIND),
        ).rowcount
        conn.commit()
    current = approval_service.get(approval_id)
    if not claimed:
        raise APIError("NOT_PENDING", f"This request is already {current['status']}", 409)
    row = current
    audit_service.write_event(
        actor_type="user", actor_id=session["user_id"],
        action="approval_approved" if approve else "approval_rejected",
        resource_type="approval", resource_id=approval_id, result="success",
        details={"proposed_by": row["proposed_by"], "kind": KIND, "note": note},
    )
    if not approve:
        approval_service._record_outcome(row, "rejected", session["user_id"])
        return approval_service.public_view(row)
    threading.Thread(target=_run_approved, args=(approval_id, session["user_id"]), daemon=True).start()
    return approval_service.public_view(row)


def _authority_for(actor: dict):
    """The same kind of authority the caller had, rebuilt now so access changes since the request count."""
    from app.security.effective_authority import permanent_authority, system_authority

    if actor["type"] == "agent":
        from app.security.scope_enforcer import build_agent_context
        from app.services import agent_service

        agent = agent_service.get_agent_by_id(actor["id"])
        if not agent or not agent.get("is_active"):
            raise APIError("AGENT_INACTIVE", "The agent that asked is no longer active", 403)
        return permanent_authority(build_agent_context(agent))
    if actor["type"] == "user":
        from app.security.context import build_user_context

        with get_db() as conn:
            user = conn.execute("SELECT id, role, is_active FROM users WHERE id = ?", (actor["id"],)).fetchone()
        if not user or not user["is_active"]:
            raise APIError("USER_INACTIVE", "The person that asked is no longer active", 403)
        return permanent_authority(build_user_context({"user_id": user["id"], "role": user["role"]}))
    return system_authority(actor.get("reason") or "an approved request")


def _finish(approval_id: str, status: str, *, result: Any = None, error: Optional[str] = None) -> None:
    from app.services import audit_service

    stored = None
    if result is not None:
        text = json.dumps(approval_service._redact([result])[0], default=str)
        stored = text if len(text) <= MAX_RESULT_CHARS else json.dumps(
            {"truncated": True, "preview": text[:MAX_RESULT_CHARS]}
        )
    with get_db() as conn:
        conn.execute(
            "UPDATE approvals SET status = ?, result_json = ?, error = ? WHERE id = ? AND status = 'executing'",
            (status, stored, error, approval_id),
        )
        conn.commit()
    row = approval_service.get(approval_id)
    audit_service.write_event(
        actor_type="user", actor_id=row["decided_by"] or "unknown",
        action="approval_run_finished", resource_type="approval", resource_id=approval_id,
        result="success" if status == "done" else "failure",
        details={"proposed_by": row["proposed_by"], "kind": KIND, "error": error},
    )
    approval_service._record_outcome(row, status, row["decided_by"] or "")


def _run_approved(approval_id: str, approver: str) -> None:
    """Run one approved request, once. Any problem ends it as failed, never as a retry."""
    from app.operations.connector_actions import run_connector_action
    from app.services import connector_service

    row = approval_service.get(approval_id)
    try:
        payload = payload_of(row)
        binding = connector_service.get_binding(payload["binding_id"])
        connector_type = connector_service.get_connector_type(binding["connector_type_id"]) if binding else None
        if not binding or not connector_type:
            return _finish(approval_id, "failed", error="The binding or service no longer exists; nothing ran.")
        if _binding_fingerprint(binding, connector_type) != json.loads(row["fingerprints_json"])[0]:
            return _finish(
                approval_id, "failed",
                error="The binding or its service was changed after this was requested; nothing ran. Ask again.",
            )
        authority = _authority_for(payload["actor"])
        with connector_service.gate_cleared(approval_id):
            outcome = run_connector_action(payload["binding_id"], payload["action"], payload["params"], authority)
        if not outcome.ok:
            return _finish(approval_id, "failed", error=f"{outcome.error_message}; nothing ran.")
        result = outcome.result or {}
        if result.get("success"):
            return _finish(approval_id, "done", result=result)
        return _finish(approval_id, "failed", result=result, error=str(result.get("error") or "The service reported an error."))
    except APIError as exc:
        _finish(approval_id, "failed", error=f"{exc.message}; nothing ran.")
    except Exception as exc:  # the request is over either way; the person sees why
        _finish(approval_id, "failed", error=f"The run stopped unexpectedly ({type(exc).__name__}); it will not be retried.")


def terminal_message(row: dict) -> Optional[str]:
    """Told to the agent that asked once the request is over and did not run, so it stops instead of retrying."""
    if row.get("kind") == KIND and row["status"] in ("rejected", "expired", "failed"):
        return "Do not retry this. Tell the person what you were trying to do and why it did not happen."
    return None
