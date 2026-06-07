import sqlite3

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.services import credential_service
from app.services import audit_service
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer
from app.security.rate_limiter import RL
from app.security.response_helpers import (
    success_response,
    success_response_with_headers,
    error_response,
    rate_limited_response,
    rate_limit_headers,
)


router = APIRouter(prefix="/api/credentials", tags=["credentials"])


class CreateCredentialRequest(BaseModel):
    scope: str
    name: str
    value: str
    label: Optional[str] = None
    metadata_json: Optional[str] = None
    expires_at: Optional[str] = None


class UpdateCredentialRequest(BaseModel):
    name: Optional[str] = None
    label: Optional[str] = None
    value: Optional[str] = None
    metadata_json: Optional[str] = None
    expires_at: Optional[str] = None


@router.get("/entries")
async def list_entries(
    scope: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    ctx: RequestContext = Depends(get_request_context),
):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if scope and not enforcer.can_read(scope):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    entries = credential_service.list_credentials(
        scope=scope, limit=limit, offset=offset
    )
    masked = []
    for entry in entries:
        if not enforcer.can_read(entry["scope"]):
            continue
        masked.append(
            {
                "id": entry["id"],
                "scope": entry["scope"],
                "name": entry["name"],
                "label": entry.get("label"),
                "metadata_json": entry.get("metadata_json"),
                "expires_at": entry.get("expires_at"),
                "reference_name": entry["reference_name"],
                "created_by": entry.get("created_by"),
                "created_at": entry.get("created_at"),
            }
        )
    return success_response({"entries": masked, "total": len(masked)})


@router.post("/entries")
async def create_entry(
    body: CreateCredentialRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    allowed, info = RL.check("user", ctx.user_id, "credential_create")
    if not allowed:
        return rate_limited_response(
            "RATE_LIMITED", "credential_create rate limit exceeded", **info
        )

    rate_headers = rate_limit_headers(**info)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(body.scope):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    try:
        entry = credential_service.create_credential(
            scope=body.scope,
            name=body.name,
            value_plaintext=body.value,
            label=body.label,
            metadata_json=body.metadata_json,
            expires_at=body.expires_at,
            created_by=ctx.user_id,
        )
    except sqlite3.IntegrityError:
        return error_response(
            "DUPLICATE_CREDENTIAL",
            "A credential with that name already exists in this scope. Use the stored credential instead of creating a new one.",
            409,
        )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="credential_entry_created",
        resource_type="credential",
        resource_id=entry["id"],
        result="success",
    )

    return success_response_with_headers(
        {
            "entry": {
                "id": entry["id"],
                "scope": entry["scope"],
                "name": entry["name"],
                "label": entry.get("label"),
                "metadata_json": entry.get("metadata_json"),
                "expires_at": entry.get("expires_at"),
                "reference_name": entry["reference_name"],
                "created_by": entry.get("created_by"),
            }
        },
        rate_headers,
        status_code=201,
    )


@router.get("/entries/{entry_id}")
async def get_entry(
    entry_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    entry = credential_service.get_credential(entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential entry not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(entry["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    return success_response(
        {
            "entry": {
                "id": entry["id"],
                "scope": entry["scope"],
                "name": entry["name"],
                "label": entry.get("label"),
                "metadata_json": entry.get("metadata_json"),
                "expires_at": entry.get("expires_at"),
                "reference_name": entry["reference_name"],
                "created_by": entry.get("created_by"),
                "created_at": entry.get("created_at"),
            }
        }
    )


@router.put("/entries/{entry_id}")
async def update_entry(
    entry_id: str,
    body: UpdateCredentialRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    entry = credential_service.get_credential(entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential entry not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(entry["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    updates = {}
    if body.name is not None:
        if not body.name.strip():
            return error_response(
                "INVALID_NAME", "Credential entry name cannot be empty", 400
            )
        updates["name"] = body.name
    if body.label is not None:
        updates["label"] = body.label
    if body.value is not None:
        from app.security.encryption import encrypt_value

        updates["value_encrypted"] = encrypt_value(body.value)
    if body.metadata_json is not None:
        updates["metadata_json"] = body.metadata_json
    if body.expires_at is not None:
        updates["expires_at"] = body.expires_at

    credential_service.update_credential(entry_id, **updates)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="credential_entry_updated",
        resource_type="credential",
        resource_id=entry_id,
        result="success",
    )

    return success_response({"message": "Credential entry updated"})


@router.delete("/entries/{entry_id}")
async def delete_entry(
    entry_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    entry = credential_service.get_credential(entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential entry not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(entry["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    credential_service.delete_credential(entry_id)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="credential_entry_deleted",
        resource_type="credential",
        resource_id=entry_id,
        result="success",
    )
    return success_response({"message": "Credential entry deleted"})


@router.post("/entries/{entry_id}/reference")
async def get_reference(
    entry_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    entry = credential_service.get_credential(entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential entry not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(entry["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="credential_reference",
        resource_type="credential",
        resource_id=entry_id,
        result="success",
    )

    return success_response({"reference_name": entry["reference_name"]})


@router.post("/entries/{entry_id}/reveal")
async def reveal_entry(
    entry_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    entry = credential_service.get_credential(entry_id)
    if not entry:
        return error_response("NOT_FOUND", "Credential entry not found", 404)

    if ctx.actor_type != "user":
        return error_response("FORBIDDEN", "User session required for reveal", 403)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(entry["scope"]):
        return error_response(
            "SCOPE_DENIED", "Access denied to this credential entry", 403
        )

    plaintext = credential_service.resolve_reference(entry["reference_name"])
    if plaintext is None:
        return error_response("RESOLVE_FAILED", "Could not resolve credential", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="credential_reveal",
        resource_type="credential",
        resource_id=entry_id,
        result="success",
    )

    return success_response({"value": plaintext})


@router.get("/scopes")
async def list_scopes(ctx: RequestContext = Depends(get_request_context)):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    all_scopes = credential_service.get_credential_scopes()
    allowed = [s for s in all_scopes if enforcer.can_read(s)]
    return success_response({"scopes": allowed})


@router.post("/rotate")
async def rotate_key(
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response(
            "FORBIDDEN", "Admin access required for key rotation", 403
        )

    from app.services import credential_rotation_service

    ok, msg, details = credential_rotation_service.rotate_key(ctx.user_id)
    if not ok:
        return error_response("ROTATION_FAILED", msg, 500)

    return success_response(
        {
            "message": msg,
            "re_encrypted_count": details.get("re_encrypted_count"),
            "keyring_size": details.get("keyring_size"),
        }
    )


@router.get("/rotate/status")
async def get_key_rotation_status(
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required", 403)

    from app.services import credential_rotation_service

    status = credential_rotation_service.get_key_status()
    return success_response({"key_status": status})


class RestoreKeyRequest(BaseModel):
    key_base64: str


@router.post("/restore-key")
async def restore_key(
    body: RestoreKeyRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required", 403)

    from app.services import credential_rotation_service

    key_bytes = body.key_base64.encode()
    ok, msg = credential_rotation_service.restore_key(ctx.user_id, key_bytes)
    if not ok:
        return error_response("RESTORE_FAILED", msg, 400)

    return success_response({"message": msg})
