from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Optional

from app.services import webhook_service, audit_service, inbound_webhook_service
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.response_helpers import (
    success_response,
    error_response,
    rate_limited_response,
)
from app.security.rate_limiter import RL
from app.security.url_validation import validate_public_url


router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])


class CreateWebhookRequest(BaseModel):
    name: str
    url: str
    secret: str
    event_types: list[str]


class UpdateWebhookRequest(BaseModel):
    name: Optional[str] = None
    url: Optional[str] = None
    secret: Optional[str] = None
    event_types: Optional[list[str]] = None
    enabled: Optional[bool] = None


def _require_admin_ctx(ctx: RequestContext = Depends(get_request_context)) -> RequestContext:
    if not ctx.is_admin:
        raise __import__("fastapi").HTTPException(status_code=403, detail="Admin access required")
    return ctx


@router.post("")
async def create_webhook(
    body: CreateWebhookRequest,
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    if not body.name.strip():
        return error_response("INVALID_NAME", "Name is required", 400)
    if not body.secret.strip():
        return error_response("INVALID_SECRET", "Secret is required", 400)
    if not body.event_types:
        return error_response("INVALID_EVENT_TYPES", "At least one event type is required", 400)

    try:
        validate_public_url(body.url)
    except ValueError as exc:
        return error_response("INVALID_URL", str(exc), 400)

    unknown = [e for e in body.event_types if e not in webhook_service.WEBHOOK_EVENT_TYPES]
    if unknown:
        return error_response("INVALID_EVENT_TYPES", f"Unknown event types: {unknown}", 400)

    webhook = webhook_service.create_webhook(
        name=body.name.strip(),
        url=body.url.strip(),
        secret_plaintext=body.secret,
        event_types=body.event_types,
        created_by=ctx.user_id or ctx.actor_id or "unknown",
    )
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="webhook_created",
        resource_type="webhook",
        resource_id=webhook["id"],
        result="success",
        details={"name": webhook["name"], "url": webhook["url"], "event_types": webhook["event_types"]},
    )
    return success_response({"webhook": webhook}, status_code=201)


@router.get("")
async def list_webhooks(
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    webhooks = webhook_service.list_webhooks()
    return success_response({"webhooks": webhooks, "total": len(webhooks)})


@router.get("/{webhook_id}")
async def get_webhook(
    webhook_id: str,
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    webhook = webhook_service.get_webhook(webhook_id)
    if not webhook:
        return error_response("NOT_FOUND", "Webhook not found", 404)
    return success_response({"webhook": webhook})


@router.put("/{webhook_id}")
async def update_webhook(
    webhook_id: str,
    body: UpdateWebhookRequest,
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    webhook = webhook_service.get_webhook(webhook_id)
    if not webhook:
        return error_response("NOT_FOUND", "Webhook not found", 404)

    if body.url is not None:
        try:
            validate_public_url(body.url)
        except ValueError as exc:
            return error_response("INVALID_URL", str(exc), 400)

    if body.event_types is not None:
        unknown = [e for e in body.event_types if e not in webhook_service.WEBHOOK_EVENT_TYPES]
        if unknown:
            return error_response("INVALID_EVENT_TYPES", f"Unknown event types: {unknown}", 400)

    updated = webhook_service.update_webhook(
        webhook_id,
        name=body.name,
        url=body.url,
        secret_plaintext=body.secret,
        event_types=body.event_types,
        enabled=body.enabled,
    )
    if not updated:
        return error_response("UPDATE_FAILED", "Update failed", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="webhook_updated",
        resource_type="webhook",
        resource_id=webhook_id,
        result="success",
        details={"name": body.name, "enabled": body.enabled},
    )
    return success_response({"webhook": webhook_service.get_webhook(webhook_id)})


@router.delete("/{webhook_id}")
async def delete_webhook(
    webhook_id: str,
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    webhook = webhook_service.get_webhook(webhook_id)
    if not webhook:
        return error_response("NOT_FOUND", "Webhook not found", 404)

    deleted = webhook_service.delete_webhook(webhook_id)
    if not deleted:
        return error_response("DELETE_FAILED", "Delete failed", 500)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="webhook_deleted",
        resource_type="webhook",
        resource_id=webhook_id,
        result="success",
        details={"name": webhook["name"]},
    )
    return success_response({"message": "Webhook deleted"})


class TestWebhookRequest(BaseModel):
    event_type: Optional[str] = None


@router.post("/{webhook_id}/test")
async def test_webhook(
    webhook_id: str,
    body: TestWebhookRequest = TestWebhookRequest(),
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    webhook = webhook_service.get_webhook(webhook_id)
    if not webhook:
        return error_response("NOT_FOUND", "Webhook not found", 404)

    if body.event_type and body.event_type not in webhook_service.WEBHOOK_EVENT_TYPES:
        return error_response("INVALID_EVENT_TYPE", f"Unknown event type: {body.event_type}", 400)

    result = webhook_service.test_delivery(webhook_id, event_type=body.event_type)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="webhook_test_delivery",
        resource_type="webhook",
        resource_id=webhook_id,
        result="success" if result.get("ok") else "failure",
        details=result,
    )
    return success_response(result)


# ---------------------------------------------------------------------------
# Inbound webhook — key management (admin) and receive endpoint (key auth)
# ---------------------------------------------------------------------------


@router.get("/inbound/key/status")
async def inbound_key_status(
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    row = inbound_webhook_service.get_active_key_row()
    if not row:
        return success_response({"has_key": False, "created_at": None, "rotated_at": None})
    return success_response({
        "has_key": True,
        "created_at": row["created_at"],
        "rotated_at": row["rotated_at"],
    })


@router.post("/inbound/key")
async def generate_inbound_key(
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    try:
        plaintext = inbound_webhook_service.generate_key()
    except ValueError as exc:
        return error_response("KEY_EXISTS", str(exc), 409)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="inbound_key_generated",
        resource_type="inbound_webhook_key",
    )
    return success_response({"key": plaintext, "note": "Store this key — it will not be shown again."}, status_code=201)


@router.post("/inbound/key/rotate")
async def rotate_inbound_key(
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    try:
        plaintext = inbound_webhook_service.rotate_key()
    except ValueError as exc:
        return error_response("NO_KEY", str(exc), 404)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="inbound_key_rotated",
        resource_type="inbound_webhook_key",
    )
    return success_response({"key": plaintext, "note": "Store this key — it will not be shown again."})


class InboundWebhookRequest(BaseModel):
    event_type: str
    workspace: Optional[str] = None
    activity_id: Optional[str] = None
    assigned_agent_id: Optional[str] = None
    task_description: Optional[str] = None
    memory_scope: Optional[str] = None
    status: Optional[str] = None
    reason: Optional[str] = None
    note: Optional[str] = None


@router.post("/inbound")
async def receive_inbound(
    body: InboundWebhookRequest,
    request: Request,
):
    client_ip = request.client.host if request.client else "unknown"
    allowed, info = RL.check("ip", client_ip, "inbound_webhook")
    if not allowed:
        return rate_limited_response(
            "RATE_LIMITED", "Too many inbound requests", **info
        )

    key_header = request.headers.get("X-Agent-Core-Inbound-Key", "")
    if not key_header or not inbound_webhook_service.verify_key(key_header):
        ip = request.client.host if request.client else None
        audit_service.write_event(
            actor_type="system",
            actor_id=None,
            action="inbound_webhook_rejected",
            resource_type="inbound_webhook",
            result="failure",
            details={"event_type": body.event_type, "reason": "invalid_key"},
            ip_address=ip,
        )
        return error_response("UNAUTHORIZED", "Invalid or missing inbound key", 401)

    ip = request.client.host if request.client else None
    payload = body.model_dump(exclude_none=True)

    try:
        result = inbound_webhook_service.handle_inbound(body.event_type, payload, ip_address=ip)
    except PermissionError as exc:
        audit_service.write_event(
            actor_type="system",
            actor_id="inbound-webhook",
            action="inbound_webhook_rejected",
            resource_type="inbound_webhook",
            result="failure",
            details={"event_type": body.event_type, "reason": str(exc)},
            ip_address=ip,
        )
        return error_response("SCOPE_DENIED", str(exc), 403)
    except ValueError as exc:
        audit_service.write_event(
            actor_type="system",
            actor_id="inbound-webhook",
            action="inbound_webhook_rejected",
            resource_type="inbound_webhook",
            result="failure",
            details={"event_type": body.event_type, "reason": str(exc)},
            ip_address=ip,
        )
        return error_response("INVALID_PAYLOAD", str(exc), 400)

    return success_response(result)


@router.get("/{webhook_id}/deliveries")
async def list_deliveries(
    webhook_id: str,
    limit: int = 50,
    ctx: RequestContext = Depends(_require_admin_ctx),
):
    webhook = webhook_service.get_webhook(webhook_id)
    if not webhook:
        return error_response("NOT_FOUND", "Webhook not found", 404)

    deliveries = webhook_service.list_deliveries(webhook_id, limit=min(limit, 200))
    return success_response({"deliveries": deliveries, "total": len(deliveries)})
