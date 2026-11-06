"""Approvals: admin changes an agent proposes and a person decides.

An agent with the admin_propose capability submits a bundle of named admin
operations (see app.operations.admin_registry). Nothing runs until a signed-in
admin approves it; then every operation runs as that person, in one
all-or-nothing database unit, and the audit records who approved and which
agent proposed. Agents can read a proposal's status but can never decide one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import timedelta
from typing import Any, Optional

from app.database import all_or_nothing, get_db
from app.operations import admin_registry
from app.security.encryption import decrypt_value, encrypt_value
from app.security.exceptions import APIError
from app.time_utils import utc_now, utc_now_iso

DEFAULT_EXPIRY_HOURS = 24
MAX_OPERATIONS = 20
# A flooded queue is the way to slip a real request past a one-tap approver, so
# an agent and the whole server can only hold this many pending at once.
MAX_PENDING_PER_AGENT = 10
MAX_PENDING_TOTAL = 50
# An approval left "executing" this long was interrupted (a crash or restart
# mid-run). The unit rolled back with the process, so it is marked failed and
# never re-run.
STUCK_AFTER_MINUTES = 10

PUBLIC_FIELDS = (
    "id", "status", "title", "note", "scope", "proposed_by", "created_at", "expires_at",
    "decided_by", "decided_at", "decision_note", "error", "kind",
)


def _request_hash(operations: list[dict]) -> str:
    return hashlib.sha256(json.dumps(operations, sort_keys=True).encode()).hexdigest()


def _fingerprints(operations: list[dict]) -> list[Optional[str]]:
    result = []
    for op in operations:
        definition = admin_registry.get(op["name"])
        result.append(definition.fingerprint(op["params"]) if definition and definition.fingerprint else None)
    return result


def _sweep(conn) -> None:
    now = utc_now()
    conn.execute(
        "UPDATE approvals SET status = 'expired' WHERE status = 'pending' AND expires_at < ?",
        (now.isoformat(),),
    )
    conn.execute(
        "UPDATE approvals SET status = 'failed', error = 'Interrupted while running; nothing was applied' "
        "WHERE status = 'executing' AND decided_at < ?",
        ((now - timedelta(minutes=STUCK_AFTER_MINUTES)).isoformat(),),
    )


def propose(*, agent_id: str, scope: str, title: str, operations: list[dict],
            note: Optional[str] = None, execution_id: Optional[str] = None,
            expiry_hours: int = DEFAULT_EXPIRY_HOURS) -> dict:
    """Validate and store a bundle. The caller has checked capability and scope."""
    title = (title or "").strip()[:200]
    if not title:
        raise APIError("INVALID_TITLE", "A proposal needs a title", 400)
    if not isinstance(operations, list) or not operations or len(operations) > MAX_OPERATIONS:
        raise APIError("INVALID_OPERATIONS", f"Propose 1 to {MAX_OPERATIONS} operations", 400)
    cleaned = []
    for op in operations:
        if not isinstance(op, dict):
            raise APIError("INVALID_OPERATIONS", "Each operation is an object with name and params", 400)
        name, params = op.get("name"), op.get("params") or {}
        admin_registry.validate(name, params)
        cleaned.append({"name": name, "params": params})
    request_hash = _request_hash(cleaned)
    now = utc_now()
    with get_db() as conn:
        _sweep(conn)
        existing = conn.execute(
            "SELECT * FROM approvals WHERE request_hash = ? AND proposed_by = ? AND status = 'pending'",
            (request_hash, agent_id),
        ).fetchone()
        if existing:
            return _public(dict(existing))
        mine = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending' AND proposed_by = ?",
                            (agent_id,)).fetchone()[0]
        if mine >= MAX_PENDING_PER_AGENT:
            raise APIError("TOO_MANY_PENDING",
                           f"You already have {mine} approvals waiting; wait for a decision before proposing more", 429)
        total = conn.execute("SELECT COUNT(*) FROM approvals WHERE status = 'pending'").fetchone()[0]
        if total >= MAX_PENDING_TOTAL:
            raise APIError("TOO_MANY_PENDING", "Too many approvals are waiting for a person; try again later", 429)
        record = {
            "id": uuid.uuid4().hex,
            "status": "pending",
            "title": title,
            "note": (note or "").strip()[:2000] or None,
            "scope": scope,
            "proposed_by": agent_id,
            "proposed_execution_id": execution_id,
            "operations_enc": encrypt_value(json.dumps(cleaned)),
            "fingerprints_json": json.dumps(_fingerprints(cleaned)),
            "request_hash": request_hash,
            "created_at": now.isoformat(),
            "expires_at": (now + timedelta(hours=max(1, min(int(expiry_hours), 168)))).isoformat(),
        }
        conn.execute(
            f"INSERT INTO approvals ({', '.join(record)}) VALUES ({', '.join('?' for _ in record)})",
            tuple(record.values()),
        )
        from app.services.workspace_sync_service import record_change

        record_change(conn, memory_scope=scope, change_type="approval_requested",
                      resource_type="approval", resource_id=record["id"],
                      summary={"title": title, "operations": [o["name"] for o in cleaned]},
                      source_agent_id=agent_id, source_execution_id=execution_id)
        conn.commit()
    _announce(record, cleaned)
    return _public(record)


def _announce(record: dict, operations: list[dict], actor_type: str = "agent") -> None:
    from app.services import audit_service, webhook_service
    from app.services.event_stream_service import event_hub

    audit_service.write_event(
        actor_type=actor_type, actor_id=record["proposed_by"], action="approval_requested",
        resource_type="approval", resource_id=record["id"], result="success",
        details={"title": record["title"], "operations": [o["name"] for o in operations]},
    )
    event_hub.publish("approval_requested", {
        "approval_id": record["id"], "title": record["title"], "scope": record["scope"],
        "proposed_by": record["proposed_by"], "expires_at": record["expires_at"],
        "operations": [o["name"] for o in operations],
    })
    # Ids only: whatever carries the link to a person fetches the page itself.
    webhook_service.dispatch_event("approval_requested", {
        "approval_id": record["id"], "kind": record.get("kind") or "admin", "scope": record["scope"],
        "proposed_by": record["proposed_by"], "expires_at": record["expires_at"],
        "path": f"/approvals/{record['id']}",
    })


def _public(row: dict) -> dict:
    return {k: row.get(k) for k in PUBLIC_FIELDS}


def operations_of(row: dict) -> list[dict]:
    return json.loads(decrypt_value(row["operations_enc"]))


def get(approval_id: str) -> Optional[dict]:
    with get_db() as conn:
        _sweep(conn)
        conn.commit()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    return dict(row) if row else None


def public_view(row: dict, *, include_operations: bool = False) -> dict:
    view = _public(row)
    view["operations"] = [o["name"] for o in operations_of(row)]
    if include_operations:
        view["operation_details"] = operations_of(row)
    if row.get("result_json"):
        view["result"] = json.loads(row["result_json"])
    return view


def list_for_scopes(scopes: Optional[list[str]], status: Optional[str] = "pending", limit: int = 50) -> list[dict]:
    """Approvals in `scopes` (None: every scope, for an admin)."""
    if scopes is not None and not scopes:
        return []
    clauses: list[str] = []
    params: list = []
    if scopes is not None:
        clauses.append(f"scope IN ({', '.join('?' for _ in scopes)})")
        params.extend(scopes)
    if status:
        clauses.append("status = ?")
        params.append(status)
    with get_db() as conn:
        _sweep(conn)
        conn.commit()
        rows = conn.execute(
            f"SELECT * FROM approvals WHERE {' AND '.join(clauses) or '1=1'} ORDER BY created_at DESC LIMIT ?",
            (*params, max(1, min(limit, 200))),
        ).fetchall()
    return [public_view(dict(r)) for r in rows]


def decide(approval_id: str, *, session: dict, approve: bool, note: Optional[str] = None,
           person_values: Optional[dict] = None, request: Any = None) -> dict:
    """Approve (and run) or reject a pending approval as the person in `session`."""
    from app.services import audit_service

    existing = get(approval_id)
    if existing is not None and existing.get("kind") == "connector_run":
        from app.services import connector_approval_service

        return connector_approval_service.decide(existing, session=session, approve=approve, note=note)
    if session.get("role") != "admin":
        raise APIError("FORBIDDEN", "Only an admin can decide admin approvals", 403)
    note = (note or "").strip()[:2000] or None
    decided_at = utc_now_iso()
    with get_db() as conn:
        _sweep(conn)
        claimed = conn.execute(
            "UPDATE approvals SET status = ?, decided_by = ?, decided_at = ?, decision_note = ? "
            "WHERE id = ? AND status = 'pending'",
            ("executing" if approve else "rejected", session["user_id"], decided_at, note, approval_id),
        ).rowcount
        conn.commit()
        row = conn.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
    if row is None:
        raise APIError("NOT_FOUND", "No such approval", 404)
    row = dict(row)
    if not claimed:
        raise APIError("NOT_PENDING", f"This approval is already {row['status']}", 409)
    proposed_by = row["proposed_by"]
    if not approve:
        audit_service.write_event(actor_type="user", actor_id=session["user_id"], action="approval_rejected",
                                  resource_type="approval", resource_id=approval_id, result="success",
                                  details={"proposed_by": proposed_by, "note": note})
        _record_outcome(row, "rejected", session["user_id"])
        return public_view(get(approval_id))

    operations = operations_of(row)
    if session.get("channel") == "approvals":
        widening = [o["name"] for o in operations if admin_registry.get(o["name"]).needs_dashboard_signin]
        if widening:
            # Released again: the person can open the dashboard and approve there.
            with get_db() as conn:
                conn.execute("UPDATE approvals SET status = 'pending', decided_by = NULL, decided_at = NULL, "
                             "decision_note = NULL WHERE id = ? AND status = 'executing'", (approval_id,))
                conn.commit()
            raise APIError("DASHBOARD_SIGNIN_REQUIRED",
                           f"{', '.join(widening)} widens what an agent can do, so sign in to the dashboard to approve it", 403)
    for index, op in enumerate(operations):
        definition = admin_registry.get(op["name"])
        for field_name in definition.person_fields:
            value = (person_values or {}).get(f"{index}.{field_name}")
            if not value:
                return _fail(row, session, f"Operation {index + 1} ({op['name']}) needs {field_name}, entered when approving")
            op["params"][field_name] = value
    if _fingerprints(operations) != json.loads(row["fingerprints_json"]):
        return _fail(row, session, "Something this approval changes was modified after it was proposed; nothing was applied. Ask for a fresh proposal.")

    results = []
    try:
        with all_or_nothing():
            for op in operations:
                results.append({"operation": op["name"], **admin_registry.run(op["name"], op["params"], session, request)})
            # Status and effects commit together: a crash can never leave an
            # applied bundle marked as interrupted.
            with get_db() as conn:
                conn.execute("UPDATE approvals SET status = 'done', result_json = ? WHERE id = ?",
                             (json.dumps(_redact(results), default=str), approval_id))
    except admin_registry.OperationFailed as exc:
        return _fail(row, session, f"{exc.operation} was refused ({exc.status}): {_message(exc.body)}. Nothing was applied.")
    except APIError as exc:
        return _fail(row, session, f"{exc.message}. Nothing was applied.")
    audit_service.write_event(actor_type="user", actor_id=session["user_id"], action="approval_approved",
                              resource_type="approval", resource_id=approval_id, result="success",
                              details={"proposed_by": proposed_by, "operations": [o["name"] for o in operations],
                                       "note": note})
    _record_outcome(row, "done", session["user_id"])
    return public_view(get(approval_id))


def _message(body: Any) -> str:
    if isinstance(body, dict):
        return str(body.get("message") or body.get("code") or body)[:300]
    return str(body)[:300]


def _redact(results: list[dict]) -> list[dict]:
    """Results go back to the proposing agent; drop anything secret-shaped."""
    secretish = ("key", "token", "secret", "password", "value")

    def clean(value):
        if isinstance(value, dict):
            return {k: ("[redacted]" if any(s in k.lower() for s in secretish) else clean(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [clean(v) for v in value]
        return value

    return clean(results)


def _fail(row: dict, session: dict, error: str) -> dict:
    from app.services import audit_service

    with get_db() as conn:
        conn.execute("UPDATE approvals SET status = 'failed', error = ? WHERE id = ?", (error, row["id"]))
        conn.commit()
    audit_service.write_event(actor_type="user", actor_id=session["user_id"], action="approval_failed",
                              resource_type="approval", resource_id=row["id"], result="failure",
                              details={"proposed_by": row["proposed_by"], "error": error})
    _record_outcome(row, "failed", session["user_id"])
    return public_view(get(row["id"]))


def _record_outcome(row: dict, status: str, decided_by: str) -> None:
    from app.services.workspace_sync_service import record_change

    with get_db() as conn:
        record_change(conn, memory_scope=row["scope"], change_type=f"approval_{status}",
                      resource_type="approval", resource_id=row["id"],
                      summary={"title": row["title"], "status": status, "decided_by": decided_by})
        conn.commit()

