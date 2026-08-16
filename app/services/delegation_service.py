"""Database-backed, recipient-bound delegated authority."""

import hashlib
import secrets
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta

from app.database import get_db
from app.security.effective_authority import EffectiveAuthority, RESOURCE_OPERATIONS
from app.security.exceptions import APIError
from app.security.scope_utils import normalize_scope_string, validate_scope_string
from app.time_utils import utc_now, utc_now_iso

GRANT_HEADER = "X-Agent-Core-Grant"
SCOPE_RESOURCES = frozenset({"memory", "briefing"})
EXACT_RESOURCES = frozenset({"activity"})
EXACT_ACTIVITY_OPERATIONS = frozenset({"read", "update", "cancel"})
MAX_TTL = timedelta(hours=1)
CLAIM_TTL = timedelta(minutes=5)


def _hash(secret: str) -> str:
    return hashlib.sha256(secret.encode()).hexdigest()


def _parse_time(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _safe_grant(row: sqlite3.Row | dict) -> dict:
    data = dict(row)
    for key in ("secret_hash", "revoked_by_actor_id"):
        data.pop(key, None)
    return data


def _issuer_can_delegate(authority: EffectiveAuthority) -> bool:
    if authority.is_delegated:
        return False
    if authority.actor_type == "user":
        return True
    if authority.actor_type == "agent":
        with get_db() as conn:
            row = conn.execute(
                "SELECT is_active, can_delegate FROM agents WHERE id = ?", (authority.actor_id,)
            ).fetchone()
        return bool(row and row["is_active"] and row["can_delegate"])
    return False


def create_grant(
    authority: EffectiveAuthority,
    *,
    recipient_agent_id: str,
    purpose: str,
    scope_permissions: list[dict],
    resource_permissions: list[dict],
    binding_actions: list[dict],
    ttl_seconds: int,
    coordinator_agent_id: str | None = None,
    activity_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Issue an approved, claimable grant after current-authority subset checks."""
    if not _issuer_can_delegate(authority):
        raise APIError("DELEGATION_FORBIDDEN", "Actor may not issue delegated grants", 403)
    if not purpose.strip() or ttl_seconds < 1 or timedelta(seconds=ttl_seconds) > MAX_TTL:
        raise APIError("INVALID_GRANT", "Purpose and a TTL of at most one hour are required", 400)
    if not (scope_permissions or resource_permissions or binding_actions):
        raise APIError("INVALID_GRANT", "At least one explicit permission is required", 400)

    now = utc_now()
    with get_db() as conn:
        recipient = conn.execute(
            "SELECT is_active FROM agents WHERE id = ?", (recipient_agent_id,)
        ).fetchone()
        if not recipient or not recipient["is_active"]:
            raise APIError("INVALID_RECIPIENT", "Recipient agent is unavailable", 400)
        if coordinator_agent_id:
            coordinator = conn.execute(
                "SELECT is_active FROM agents WHERE id = ?", (coordinator_agent_id,)
            ).fetchone()
            if not coordinator or not coordinator["is_active"]:
                raise APIError("INVALID_COORDINATOR", "Coordinator agent is unavailable", 400)

        normalized_scopes: set[tuple[str, str, str]] = set()
        for permission in scope_permissions:
            resource = permission.get("resource_type", "")
            operation = permission.get("operation", "")
            raw_scope = permission.get("scope", "")
            if resource not in SCOPE_RESOURCES or operation not in RESOURCE_OPERATIONS[resource]:
                raise APIError("INVALID_PERMISSION", "Unknown scope permission", 400)
            if not validate_scope_string(raw_scope):
                raise APIError("INVALID_PERMISSION", "Invalid permission scope", 400)
            scope = normalize_scope_string(raw_scope)
            if not authority.can(resource, operation, scope=scope):
                raise APIError("DELEGATION_EXCEEDS_AUTHORITY", "Requested permission exceeds issuer authority", 403)
            normalized_scopes.add((resource, operation, scope))

        normalized_resources: set[tuple[str, str, str]] = set()
        for permission in resource_permissions:
            resource = permission.get("resource_type", "")
            operation = permission.get("operation", "")
            resource_id = str(permission.get("resource_id", "")).strip()
            if resource not in EXACT_RESOURCES or operation not in EXACT_ACTIVITY_OPERATIONS or not resource_id:
                raise APIError("INVALID_PERMISSION", "Unknown exact-resource permission", 400)
            activity = conn.execute(
                "SELECT memory_scope, user_id, assigned_agent_id FROM agent_activity WHERE id = ?", (resource_id,)
            ).fetchone()
            if not activity or not authority.can(resource, operation, scope=activity["memory_scope"]):
                raise APIError("DELEGATION_EXCEEDS_AUTHORITY", "Requested resource exceeds issuer authority", 403)
            if activity["assigned_agent_id"] != recipient_agent_id and activity["user_id"] != authority.principal_user_id:
                raise APIError("INVALID_PERMISSION", "Activity is not assigned to the recipient or principal", 400)
            normalized_resources.add((resource, operation, resource_id))

        normalized_actions: set[tuple[str, str]] = set()
        from app.services import connector_service

        for permission in binding_actions:
            binding_id = str(permission.get("binding_id", "")).strip()
            action = str(permission.get("action", "")).strip()
            binding = connector_service.get_binding(binding_id)
            connector_type = connector_service.get_connector_type(binding["connector_type_id"]) if binding else None
            actions = _action_names(connector_type)
            if not binding or not binding.get("enabled") or not connector_type or not connector_type.get("is_active") or not action or action not in actions:
                raise APIError("INVALID_PERMISSION", "Binding action is unavailable", 400)
            required_operation = "execute" if connector_service.action_requires_write(connector_type, action) else "read"
            if not authority.can("connector", required_operation, scope=binding["scope"]):
                raise APIError("DELEGATION_EXCEEDS_AUTHORITY", "Binding action exceeds issuer authority", 403)
            normalized_actions.add((binding_id, action))

        grant_id = secrets.token_urlsafe(18)
        principal_id = authority.principal_user_id
        if not principal_id:
            raise APIError("DELEGATION_FORBIDDEN", "A current human principal is required", 403)
        principal = conn.execute("SELECT is_active FROM users WHERE id = ?", (principal_id,)).fetchone()
        if not principal or not principal["is_active"]:
            raise APIError("DELEGATION_FORBIDDEN", "Principal is unavailable", 403)
        expires_at = now + timedelta(seconds=ttl_seconds)
        conn.execute(
            """INSERT INTO delegated_grants
               (id, issuer_actor_type, issuer_actor_id, principal_user_id, recipient_agent_id,
                coordinator_agent_id, purpose, activity_id, correlation_id, status, issued_at,
                expires_at, claim_expires_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'approved_unclaimed', ?, ?, ?)""",
            (grant_id, authority.actor_type, authority.actor_id, principal_id, recipient_agent_id,
             coordinator_agent_id, purpose.strip(), activity_id, correlation_id, utc_now_iso(),
             expires_at.isoformat(), min(expires_at, now + CLAIM_TTL).isoformat()),
        )
        conn.executemany(
            "INSERT INTO delegated_grant_scope_permissions VALUES (?, ?, ?, ?)",
            [(grant_id, *item) for item in normalized_scopes],
        )
        conn.executemany(
            "INSERT INTO delegated_grant_resource_permissions VALUES (?, ?, ?, ?)",
            [(grant_id, *item) for item in normalized_resources],
        )
        conn.executemany(
            "INSERT INTO delegated_grant_actions VALUES (?, ?, ?)",
            [(grant_id, *item) for item in normalized_actions],
        )
        conn.commit()
        row = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
    return _safe_grant(row)


def claim_grant(grant_id: str, recipient_agent_id: str) -> tuple[dict, str]:
    """Claim once; only the bound, active recipient can obtain the secret."""
    now = utc_now()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
        if not row or row["recipient_agent_id"] != recipient_agent_id:
            raise APIError("GRANT_NOT_FOUND", "Grant is not claimable", 404)
        if row["status"] != "approved_unclaimed" or now >= _parse_time(row["claim_expires_at"]):
            raise APIError("GRANT_NOT_CLAIMABLE", "Grant is not claimable", 409)
        agent = conn.execute("SELECT is_active FROM agents WHERE id = ?", (recipient_agent_id,)).fetchone()
        if not agent or not agent["is_active"]:
            raise APIError("GRANT_NOT_CLAIMABLE", "Grant is not claimable", 409)
        secret = f"ac_dg_{grant_id}.{secrets.token_urlsafe(32)}"
        changed = conn.execute(
            """UPDATE delegated_grants SET status = 'active', secret_hash = ?, claimed_at = ?
               WHERE id = ? AND status = 'approved_unclaimed'""",
            (_hash(secret), utc_now_iso(), grant_id),
        )
        if changed.rowcount != 1:
            conn.rollback()
            raise APIError("GRANT_NOT_CLAIMABLE", "Grant is not claimable", 409)
        conn.commit()
        claimed = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
    return _safe_grant(claimed), secret


def revoke_grant(grant_id: str, authority: EffectiveAuthority, reason: str | None = None) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
        if not row:
            raise APIError("GRANT_NOT_FOUND", "Grant not found", 404)
        allowed = authority.is_admin or (
            row["issuer_actor_type"] == authority.actor_type and row["issuer_actor_id"] == authority.actor_id
        ) or row["recipient_agent_id"] == authority.agent_id
        if not allowed:
            raise APIError("FORBIDDEN", "Grant is not visible", 403)
        conn.execute(
            """UPDATE delegated_grants SET status = 'revoked', revoked_at = ?,
               revoked_by_actor_id = ?, revocation_reason = ? WHERE id = ?""",
            (utc_now_iso(), authority.actor_id, reason, grant_id),
        )
        conn.commit()
        updated = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
    return _safe_grant(updated)


def get_grant(grant_id: str, authority: EffectiveAuthority) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
    if not row or not _grant_visible(row, authority):
        raise APIError("GRANT_NOT_FOUND", "Grant not found", 404)
    return _safe_grant(row)


def list_grants(authority: EffectiveAuthority) -> list[dict]:
    if authority.is_delegated:
        raise APIError("FORBIDDEN", "Grant administration requires permanent authority", 403)
    with get_db() as conn:
        if authority.is_admin:
            rows = conn.execute("SELECT * FROM delegated_grants ORDER BY issued_at DESC").fetchall()
        elif authority.actor_type == "agent":
            rows = conn.execute(
                """SELECT * FROM delegated_grants
                   WHERE recipient_agent_id = ? OR (issuer_actor_type = 'agent' AND issuer_actor_id = ?)
                   ORDER BY issued_at DESC""",
                (authority.agent_id, authority.actor_id),
            ).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM delegated_grants
                   WHERE principal_user_id = ? OR (issuer_actor_type = 'user' AND issuer_actor_id = ?)
                   ORDER BY issued_at DESC""",
                (authority.principal_user_id, authority.actor_id),
            ).fetchall()
    return [_safe_grant(row) for row in rows]


def _grant_visible(row, authority: EffectiveAuthority) -> bool:
    if authority.is_delegated:
        return False
    return bool(
        authority.is_admin
        or row["principal_user_id"] == authority.principal_user_id
        or (row["issuer_actor_type"] == authority.actor_type and row["issuer_actor_id"] == authority.actor_id)
        or row["recipient_agent_id"] == authority.agent_id
    )


def build_delegated_authority(context, header_value: str) -> EffectiveAuthority:
    """Authenticate and revalidate a grant for its bound recipient on every request."""
    if context.actor_type != "agent" or not header_value.startswith("ac_dg_") or "." not in header_value:
        raise APIError("INVALID_GRANT", "Invalid delegated credential", 401)
    grant_id = header_value.removeprefix("ac_dg_").split(".", 1)[0]
    now = utc_now()
    with get_db() as conn:
        row = conn.execute("SELECT * FROM delegated_grants WHERE id = ?", (grant_id,)).fetchone()
        if not row or row["recipient_agent_id"] != context.agent_id or not secrets.compare_digest(row["secret_hash"] or "", _hash(header_value)):
            raise APIError("INVALID_GRANT", "Invalid delegated credential", 401)
        if row["status"] != "active":
            raise APIError("GRANT_INACTIVE", "Delegated grant is inactive", 403)
        if now >= _parse_time(row["expires_at"]):
            conn.execute("UPDATE delegated_grants SET status = 'expired' WHERE id = ? AND status = 'active'", (grant_id,))
            conn.commit()
            raise APIError("GRANT_EXPIRED", "Delegated grant has expired", 403)
        principal = conn.execute("SELECT is_active FROM users WHERE id = ?", (row["principal_user_id"],)).fetchone()
        recipient = conn.execute("SELECT is_active FROM agents WHERE id = ?", (context.agent_id,)).fetchone()
        if not principal or not principal["is_active"] or not recipient or not recipient["is_active"]:
            raise APIError("GRANT_INVALIDATED", "Delegated grant is no longer valid", 403)
        if row["issuer_actor_type"] == "agent":
            issuer = conn.execute("SELECT * FROM agents WHERE id = ?", (row["issuer_actor_id"],)).fetchone()
            if not issuer or not issuer["is_active"]:
                raise APIError("GRANT_INVALIDATED", "Delegated grant is no longer valid", 403)
            from app.security.scope_enforcer import build_agent_context
            issuer_authority = EffectiveAuthority(build_agent_context(dict(issuer)))
        else:
            issuer = conn.execute("SELECT id, role, is_active FROM users WHERE id = ?", (row["issuer_actor_id"],)).fetchone()
            if not issuer or not issuer["is_active"]:
                raise APIError("GRANT_INVALIDATED", "Delegated grant is no longer valid", 403)
            from app.security.context import build_user_context
            issuer_authority = EffectiveAuthority(build_user_context({"user_id": issuer["id"], "role": issuer["role"]}))
        scopes = conn.execute("SELECT resource_type, operation, scope FROM delegated_grant_scope_permissions WHERE grant_id = ?", (grant_id,)).fetchall()
        resources = conn.execute("SELECT resource_type, operation, resource_id FROM delegated_grant_resource_permissions WHERE grant_id = ?", (grant_id,)).fetchall()
        actions = conn.execute("SELECT binding_id, action FROM delegated_grant_actions WHERE grant_id = ?", (grant_id,)).fetchall()
        if any(not issuer_authority.can(p["resource_type"], p["operation"], scope=p["scope"]) for p in scopes):
            raise APIError("GRANT_INVALIDATED", "Issuer authority has changed", 403)
        for permission in resources:
            activity = conn.execute(
                "SELECT memory_scope, user_id, assigned_agent_id FROM agent_activity WHERE id = ?",
                (permission["resource_id"],),
            ).fetchone()
            if not activity or not issuer_authority.can("activity", permission["operation"], scope=activity["memory_scope"]):
                raise APIError("GRANT_INVALIDATED", "Activity authority has changed", 403)
            if activity["assigned_agent_id"] != context.agent_id and activity["user_id"] != row["principal_user_id"]:
                raise APIError("GRANT_INVALIDATED", "Activity assignment has changed", 403)
        from app.services import connector_service
        for permission in actions:
            binding = connector_service.get_binding(permission["binding_id"])
            connector_type = connector_service.get_connector_type(binding["connector_type_id"]) if binding else None
            available = _action_names(connector_type)
            required_operation = "execute" if connector_service.action_requires_write(connector_type or {}, permission["action"]) else "read"
            if not binding or not binding.get("enabled") or not connector_type or not connector_type.get("is_active") or permission["action"] not in available or not issuer_authority.can("connector", required_operation, scope=binding["scope"]):
                raise APIError("GRANT_INVALIDATED", "Binding authority has changed", 403)
    # Legacy scope-only checks must fail closed. Only resource-named
    # EffectiveAuthority checks can authorize a delegated operation.
    delegated_context = replace(
        context, read_scopes=[], write_scopes=[], default_recall_scopes=[]
    )
    return EffectiveAuthority(
        context=delegated_context, grant_id=grant_id, principal_user_id=row["principal_user_id"],
        issuer_actor_id=row["issuer_actor_id"], coordinator_agent_id=row["coordinator_agent_id"],
        correlation_id=row["correlation_id"], expires_at=row["expires_at"],
        scope_permissions=frozenset((p["resource_type"], p["operation"], p["scope"]) for p in scopes),
        resource_permissions=frozenset((p["resource_type"], p["operation"], p["resource_id"]) for p in resources),
        binding_actions=frozenset((p["binding_id"], p["action"]) for p in actions),
    )


def _action_names(connector_type: dict | None) -> set[str]:
    names: set[str] = set()
    for item in (connector_type or {}).get("supported_actions") or []:
        name = item.get("name") if isinstance(item, dict) else item
        if isinstance(name, str) and name:
            names.add(name)
    return names - set((connector_type or {}).get("disabled_actions") or [])


def _notify_request(event: str, request: dict) -> None:
    """Announce a request-lifecycle change to the live dashboard and webhooks.

    Grants have a five-minute claim window and an hour-long TTL, so an approval
    flow nobody notices always times out. Notification lives here, in the
    service, because both REST and MCP create and decide requests.
    """
    from app.services import webhook_service
    from app.services.event_stream_service import event_hub

    payload = {
        "request_id": request.get("id"),
        "status": request.get("status"),
        "requester_actor_type": request.get("requester_actor_type"),
        "requester_actor_id": request.get("requester_actor_id"),
        "recipient_agent_id": request.get("recipient_agent_id"),
        "target_user_id": request.get("target_user_id"),
        "purpose": request.get("purpose"),
        "ttl_seconds": request.get("ttl_seconds"),
        "scope_permission_count": len(request.get("scope_permissions") or []),
        "resource_permission_count": len(request.get("resource_permissions") or []),
        "binding_action_count": len(request.get("binding_actions") or []),
        "decided_by_actor_id": request.get("decided_by_actor_id"),
        "decision_reason": request.get("decision_reason"),
        "grant_id": request.get("grant_id"),
        "created_at": request.get("created_at"),
        "decided_at": request.get("decided_at"),
    }
    event_hub.publish(event, payload)
    webhook_service.dispatch_event(event, payload)


def create_request(
    authority: EffectiveAuthority,
    *,
    recipient_agent_id: str,
    purpose: str,
    ttl_seconds: int,
    scope_permissions: list[dict],
    resource_permissions: list[dict],
    binding_actions: list[dict],
    activity_id: str | None = None,
    correlation_id: str | None = None,
) -> dict:
    """Record requested authority without treating it as granted authority."""
    if authority.is_delegated or authority.actor_type not in ("user", "agent"):
        raise APIError("FORBIDDEN", "Delegated requests require permanent authentication", 403)
    if not purpose.strip() or ttl_seconds < 1 or ttl_seconds > 3600:
        raise APIError("INVALID_REQUEST", "Purpose and a TTL of at most one hour are required", 400)
    scopes, resources, actions = _normalize_requested_permissions(
        scope_permissions, resource_permissions, binding_actions
    )
    if not (scopes or resources or actions):
        raise APIError("INVALID_REQUEST", "At least one explicit permission is required", 400)
    with get_db() as conn:
        recipient = conn.execute(
            "SELECT is_active, default_user_id, owner_user_id FROM agents WHERE id = ?",
            (recipient_agent_id,),
        ).fetchone()
        if not recipient or not recipient["is_active"]:
            raise APIError("INVALID_RECIPIENT", "Recipient agent is unavailable", 400)
        target_user_id = recipient["default_user_id"] or recipient["owner_user_id"]
        target = conn.execute("SELECT is_active FROM users WHERE id = ?", (target_user_id,)).fetchone()
        if not target or not target["is_active"]:
            raise APIError("INVALID_RECIPIENT", "Recipient principal is unavailable", 400)
        request_id = secrets.token_urlsafe(18)
        coordinator_id = authority.agent_id if authority.actor_type == "agent" else None
        conn.execute(
            """INSERT INTO delegation_requests
               (id, requester_actor_type, requester_actor_id, target_user_id, recipient_agent_id,
                coordinator_agent_id, purpose, ttl_seconds, activity_id, correlation_id, status, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)""",
            (request_id, authority.actor_type, authority.actor_id, target_user_id,
             recipient_agent_id, coordinator_id, purpose.strip(), ttl_seconds,
             activity_id, correlation_id, utc_now_iso()),
        )
        conn.executemany(
            "INSERT INTO delegation_request_scope_permissions VALUES (?, ?, ?, ?)",
            [(request_id, *item) for item in scopes],
        )
        conn.executemany(
            "INSERT INTO delegation_request_resource_permissions VALUES (?, ?, ?, ?)",
            [(request_id, *item) for item in resources],
        )
        conn.executemany(
            "INSERT INTO delegation_request_actions VALUES (?, ?, ?)",
            [(request_id, *item) for item in actions],
        )
        conn.commit()
    request = get_request(request_id, authority)
    _notify_request("delegation_request_created", request)
    return request


def _normalize_requested_permissions(scope_permissions, resource_permissions, binding_actions):
    scopes: set[tuple[str, str, str]] = set()
    for item in scope_permissions:
        resource, operation, raw_scope = item.get("resource_type", ""), item.get("operation", ""), item.get("scope", "")
        if resource not in SCOPE_RESOURCES or operation not in RESOURCE_OPERATIONS[resource] or not validate_scope_string(raw_scope):
            raise APIError("INVALID_PERMISSION", "Unknown scope permission", 400)
        scopes.add((resource, operation, normalize_scope_string(raw_scope)))
    resources: set[tuple[str, str, str]] = set()
    for item in resource_permissions:
        resource, operation = item.get("resource_type", ""), item.get("operation", "")
        resource_id = str(item.get("resource_id", "")).strip()
        if resource not in EXACT_RESOURCES or operation not in EXACT_ACTIVITY_OPERATIONS or not resource_id:
            raise APIError("INVALID_PERMISSION", "Unknown exact-resource permission", 400)
        resources.add((resource, operation, resource_id))
    actions: set[tuple[str, str]] = set()
    for item in binding_actions:
        binding_id, action = str(item.get("binding_id", "")).strip(), str(item.get("action", "")).strip()
        if not binding_id or not action:
            raise APIError("INVALID_PERMISSION", "Invalid binding action", 400)
        actions.add((binding_id, action))
    return scopes, resources, actions


def _request_permissions(conn, request_id: str) -> tuple[list[dict], list[dict], list[dict]]:
    scopes = [dict(row) for row in conn.execute(
        "SELECT resource_type, operation, scope FROM delegation_request_scope_permissions WHERE request_id = ?", (request_id,)
    ).fetchall()]
    resources = [dict(row) for row in conn.execute(
        "SELECT resource_type, operation, resource_id FROM delegation_request_resource_permissions WHERE request_id = ?", (request_id,)
    ).fetchall()]
    actions = [dict(row) for row in conn.execute(
        "SELECT binding_id, action FROM delegation_request_actions WHERE request_id = ?", (request_id,)
    ).fetchall()]
    return scopes, resources, actions


def _request_visible(row, authority: EffectiveAuthority) -> bool:
    return bool(
        not authority.is_delegated and (
            authority.is_admin
            or row["target_user_id"] == authority.principal_user_id
            or (row["requester_actor_type"] == authority.actor_type and row["requester_actor_id"] == authority.actor_id)
            or row["recipient_agent_id"] == authority.agent_id
        )
    )


def get_request(request_id: str, authority: EffectiveAuthority) -> dict:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM delegation_requests WHERE id = ?", (request_id,)).fetchone()
        if not row or not _request_visible(row, authority):
            raise APIError("REQUEST_NOT_FOUND", "Delegation request not found", 404)
        scopes, resources, actions = _request_permissions(conn, request_id)
    result = dict(row)
    result.update(scope_permissions=scopes, resource_permissions=resources, binding_actions=actions)
    return result


def list_requests(authority: EffectiveAuthority) -> list[dict]:
    if authority.is_delegated:
        raise APIError("FORBIDDEN", "Request administration requires permanent authority", 403)
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM delegation_requests ORDER BY created_at DESC").fetchall()
    return [get_request(row["id"], authority) for row in rows if _request_visible(row, authority)]


def approve_request(
    request_id: str,
    authority: EffectiveAuthority,
    *,
    scope_permissions: list[dict] | None = None,
    resource_permissions: list[dict] | None = None,
    binding_actions: list[dict] | None = None,
) -> dict:
    request = get_request(request_id, authority)
    if request["status"] != "pending":
        raise APIError("REQUEST_DECIDED", "Delegation request has already been decided", 409)
    requested_sets = _normalize_requested_permissions(
        request["scope_permissions"], request["resource_permissions"], request["binding_actions"]
    )
    approved = (
        scope_permissions if scope_permissions is not None else request["scope_permissions"],
        resource_permissions if resource_permissions is not None else request["resource_permissions"],
        binding_actions if binding_actions is not None else request["binding_actions"],
    )
    approved_sets = _normalize_requested_permissions(*approved)
    if any(not approved_sets[i].issubset(requested_sets[i]) for i in range(3)):
        raise APIError("APPROVAL_EXPANDS_REQUEST", "Approval may only narrow requested authority", 400)
    grant = create_grant(
        authority, recipient_agent_id=request["recipient_agent_id"], purpose=request["purpose"],
        scope_permissions=approved[0], resource_permissions=approved[1], binding_actions=approved[2],
        ttl_seconds=request["ttl_seconds"], coordinator_agent_id=request["coordinator_agent_id"],
        activity_id=request["activity_id"], correlation_id=request["correlation_id"],
    )
    with get_db() as conn:
        changed = conn.execute(
            """UPDATE delegation_requests SET status = 'approved', decided_at = ?,
               decided_by_actor_type = ?, decided_by_actor_id = ?, grant_id = ?
               WHERE id = ? AND status = 'pending'""",
            (utc_now_iso(), authority.actor_type, authority.actor_id, grant["id"], request_id),
        )
        if changed.rowcount != 1:
            conn.execute("DELETE FROM delegated_grants WHERE id = ?", (grant["id"],))
            conn.commit()
            raise APIError("REQUEST_DECIDED", "Delegation request has already been decided", 409)
        conn.commit()
    decided = get_request(request_id, authority)
    _notify_request("delegation_request_approved", decided)
    return {"request": decided, "grant": grant}


def deny_request(request_id: str, authority: EffectiveAuthority, reason: str | None) -> dict:
    request = get_request(request_id, authority)
    if request["status"] != "pending":
        raise APIError("REQUEST_DECIDED", "Delegation request has already been decided", 409)
    if not _issuer_can_delegate(authority):
        raise APIError("DELEGATION_FORBIDDEN", "Actor may not decide delegation requests", 403)
    with get_db() as conn:
        changed = conn.execute(
            """UPDATE delegation_requests SET status = 'denied', decided_at = ?,
               decided_by_actor_type = ?, decided_by_actor_id = ?, decision_reason = ?
               WHERE id = ? AND status = 'pending'""",
            (utc_now_iso(), authority.actor_type, authority.actor_id, reason, request_id),
        )
        conn.commit()
    if changed.rowcount != 1:
        raise APIError("REQUEST_DECIDED", "Delegation request has already been decided", 409)
    denied = get_request(request_id, authority)
    _notify_request("delegation_request_denied", denied)
    return denied
