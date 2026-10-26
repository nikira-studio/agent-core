"""Credential requests: an agent names the secret it needs, a person supplies it.

Agents must never ask a person to paste a raw secret into the conversation: the
value would then live in the model's context and transcript. A request records
what is needed and why; the owner fills it in the dashboard; the agent gets the
AC_SECRET reference back by asking again. The plaintext only travels from the
person's browser to the vault.
"""

import secrets
import sqlite3
from typing import Optional

from app.database import get_db
from app.services import credential_service
from app.time_utils import utc_now_iso

MAX_PENDING_PER_AGENT = 20


def _row(conn, request_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM credential_requests WHERE id = ?", (request_id,)).fetchone()
    return dict(row) if row else None


def _existing_credential(scope: str, name: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT id, reference_name FROM credentials WHERE scope = ? AND name = ?",
            (scope, name),
        ).fetchone()
    return dict(row) if row else None


def request_credential(
    *, agent_id: str, user_id: Optional[str], scope: str, name: str, purpose: str
) -> dict:
    """Open a request, or report the one already open or already filled.

    Idempotent per scope and name, so an agent checks progress by calling it
    again: pending until a person fills it, then the reference.
    """
    scope = credential_service.normalize_scope(scope)
    existing = _existing_credential(scope, name)
    if existing:
        return {"status": "fulfilled", "scope": scope, "name": name,
                "reference_name": existing["reference_name"]}
    with get_db() as conn:
        pending = conn.execute(
            "SELECT * FROM credential_requests WHERE scope = ? AND name = ? AND status = 'pending'",
            (scope, name),
        ).fetchone()
        if pending:
            return {**_public(dict(pending)), "created": False}
        latest = conn.execute(
            """SELECT * FROM credential_requests WHERE scope = ? AND name = ?
            ORDER BY created_at DESC LIMIT 1""",
            (scope, name),
        ).fetchone()
        if latest and latest["status"] == "cancelled":
            # A person said no. Asking again by itself would just nag; they can
            # still add the credential directly, which the check above finds.
            return {
                **_public(dict(latest)),
                "created": False,
                "message": "A person declined this request. Ask them before requesting it again.",
            }
        open_count = conn.execute(
            "SELECT COUNT(*) FROM credential_requests WHERE requested_by_agent_id = ? AND status = 'pending'",
            (agent_id,),
        ).fetchone()[0]
        if open_count >= MAX_PENDING_PER_AGENT:
            raise ValueError(
                f"{MAX_PENDING_PER_AGENT} requests are already waiting on a person; "
                "wait for those to be filled or cancelled"
            )
        request_id = secrets.token_urlsafe(12)
        try:
            conn.execute(
                """INSERT INTO credential_requests
                (id, scope, name, purpose, requested_by_agent_id, requested_by_user_id, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (request_id, scope, name, purpose, agent_id, user_id, utc_now_iso()),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.rollback()  # a concurrent request for the same name won the race
        row = conn.execute(
            "SELECT * FROM credential_requests WHERE scope = ? AND name = ? AND status = 'pending'",
            (scope, name),
        ).fetchone()
    return {**_public(dict(row)), "created": row["id"] == request_id}


def _public(request: dict) -> dict:
    result = {
        "request_id": request["id"],
        "status": request["status"],
        "scope": request["scope"],
        "name": request["name"],
        "purpose": request["purpose"],
        "requested_by": request["requested_by_agent_id"],
        "created_at": request["created_at"],
    }
    if request["status"] == "cancelled":
        result["status"] = "declined"
    if request["status"] == "pending":
        result["fill_in"] = "Credentials page of the Agent Core dashboard, under Requested by agents"
    return result


def list_pending() -> list[dict]:
    """Every open request, oldest first. Callers filter by what the viewer can read."""
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM credential_requests WHERE status = 'pending' ORDER BY created_at"
        ).fetchall()
    return [dict(r) for r in rows]


def get_request(request_id: str) -> Optional[dict]:
    with get_db() as conn:
        return _row(conn, request_id)


def fulfill(
    request_id: str,
    *,
    value: str,
    user_id: str,
    label: Optional[str] = None,
    expires_at: Optional[str] = None,
) -> dict:
    request = get_request(request_id)
    if not request or request["status"] != "pending":
        raise LookupError("No pending request with that id")
    entry = credential_service.create_credential(
        scope=request["scope"],
        name=request["name"],
        value_plaintext=value,
        label=label,
        expires_at=expires_at,
        created_by=user_id,
    )
    with get_db() as conn:
        conn.execute(
            """UPDATE credential_requests SET status = 'fulfilled', credential_id = ?,
            resolved_at = ?, resolved_by = ? WHERE id = ?""",
            (entry["id"], utc_now_iso(), user_id, request_id),
        )
        conn.commit()
    return entry


def cancel(request_id: str, *, user_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute(
            """UPDATE credential_requests SET status = 'cancelled', resolved_at = ?, resolved_by = ?
            WHERE id = ? AND status = 'pending'""",
            (utc_now_iso(), user_id, request_id),
        )
        conn.commit()
        return cursor.rowcount > 0
