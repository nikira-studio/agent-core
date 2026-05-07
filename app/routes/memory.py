import json
from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from typing import Optional

from app.services import memory_service, audit_service

try:
    from app.services import embedding_service
    _EMBED_AVAILABLE = True
except Exception:
    _EMBED_AVAILABLE = False
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer
from app.security.rate_limiter import RL, CSG
from app.security.response_helpers import (
    success_response, success_response_with_headers, error_response, rate_limited_response, rate_limit_headers,
)
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS


router = APIRouter(prefix="/api/memory", tags=["memory"])


def _embedding_backend_status() -> dict:
    if not _EMBED_AVAILABLE:
        return {"backend": "unavailable", "model_configured": False}
    try:
        return embedding_service.get_embedding_backend_status()
    except Exception:
        return {"backend": "error", "model_configured": False}


def _embedding_backend_label(status: dict) -> str:
    return status.get("backend", "unknown")


def _retrieval_is_degraded(status: dict) -> bool:
    return status.get("backend") != "healthy" or not status.get("model_configured", False)


class WriteMemoryRequest(BaseModel):
    content: str
    memory_class: str
    scope: str
    domain: Optional[str] = None
    topic: Optional[str] = None
    confidence: float = 0.5
    importance: float = 0.5
    source_kind: str = "agent_inference"
    event_time: Optional[str] = None
    supersedes_id: Optional[str] = None


class SearchMemoryRequest(BaseModel):
    query: str
    scope: Optional[str] = None
    domain: Optional[str] = None
    topic: Optional[str] = None
    memory_class: Optional[str] = None
    min_confidence: float = 0.0
    limit: int = 20
    offset: int = 0
    include_retracted: bool = False
    include_superseded: bool = False


class GetMemoryRequest(BaseModel):
    scope: Optional[str] = None
    record_status: Optional[str] = None
    limit: int = 50
    offset: int = 0


@router.post("/write")
async def write_memory(
    body: WriteMemoryRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    allowed, info = RL.check("agent", ctx.agent_id, "memory_write")
    if not allowed:
        return rate_limited_response("RATE_LIMITED", "memory_write rate limit exceeded", **info)

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

    if body.memory_class not in MEMORY_CLASSES:
        return error_response("INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400)

    if body.source_kind not in SOURCE_KINDS:
        return error_response("INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400)

    if not 0.0 <= body.confidence <= 1.0:
        return error_response("INVALID_CONFIDENCE", "confidence must be between 0.0 and 1.0", 400)

    if not 0.0 <= body.importance <= 1.0:
        return error_response("INVALID_IMPORTANCE", "importance must be between 0.0 and 1.0", 400)

    if body.supersedes_id:
        old_record = memory_service.get_memory_record(body.supersedes_id)
        if not old_record:
            return error_response("NOT_FOUND", "Record to supersede not found", 404)
        if old_record["record_status"] != "active":
            return error_response("INVALID_SUPERSESSION", "Cannot supersede non-active record", 400)
        if not enforcer.can_write(old_record["scope"]):
            return error_response("SCOPE_DENIED", "Access denied to scope of record being superseded", 403)

    record, pii_flag = memory_service.write_memory(
        content=body.content,
        memory_class=body.memory_class,
        scope=body.scope,
        domain=body.domain,
        topic=body.topic,
        confidence=body.confidence,
        importance=body.importance,
        source_kind=body.source_kind,
        event_time=body.event_time,
        supersedes_id=body.supersedes_id,
    )

    if pii_flag == "PII_DETECTED":
        return error_response(
            "PII_DETECTED",
            "Content contains PII and cannot be written to shared scope",
            422,
        )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_write",
        resource_type="memory_record",
        resource_id=record["id"],
        result="success",
    )

    return success_response_with_headers({"record": record}, rate_headers, status_code=201)


@router.post("/search")
async def search_memory(
    body: SearchMemoryRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    allowed, info = RL.check("agent", ctx.agent_id, "memory_search")
    if not allowed:
        return rate_limited_response("RATE_LIMITED", "memory_search rate limit exceeded", **info)

    rate_headers = rate_limit_headers(**info)

    if not CSG.acquire(ctx.agent_id):
        return error_response("CONCURRENT_LIMIT", "Too many concurrent searches", 429)

    try:
        enforcer = ScopeEnforcer(
            ctx.read_scopes,
            ctx.write_scopes,
            ctx.agent_id,
            is_admin=ctx.is_admin,
            active_workspace_ids=ctx.active_workspace_ids,
        )

        query_text = body.query.strip()
        if len(query_text) <= 2:
            return error_response("QUERY_TOO_SHORT", "Query must be at least 2 characters", 400)

        trivial_patterns = [
            r"^(the|a|an|is|are|was|were|i|you|he|she|it|we|they)\s*$",
            r"^[.,;:!?]+$",
        ]
        import re
        for pattern in trivial_patterns:
            if re.match(pattern, query_text, re.IGNORECASE):
                return error_response("QUERY_NOISE", "Query is too trivial", 400)

        if body.memory_class and body.memory_class not in MEMORY_CLASSES:
            return error_response("INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400)

        if not 0.0 <= body.min_confidence <= 1.0:
            return error_response("INVALID_CONFIDENCE", "min_confidence must be between 0.0 and 1.0", 400)

        from app.security.pii_detector import contains_pii
        if contains_pii(query_text):
            return error_response("QUERY_NOISE", "Query contains credential-like pattern", 400)

        if body.scope:
            if not enforcer.can_read(body.scope):
                return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
            allowed_scopes = [body.scope]
        else:
            allowed_scopes = enforcer.filter_readable_scopes(ctx.read_scopes)
        if not allowed_scopes:
            embedding_status = _embedding_backend_status()
            return success_response_with_headers({
                "records": [],
                "retrieval_mode": "fts_only",
                "embedding_backend_status": _embedding_backend_label(embedding_status),
                "total": 0,
            }, rate_headers)

        records, retrieval_mode = memory_service.search_memory(
            query=body.query,
            authorized_scopes=allowed_scopes,
            domain=body.domain,
            topic=body.topic,
            memory_class=body.memory_class,
            min_confidence=body.min_confidence,
            limit=min(body.limit, 100),
            offset=body.offset,
            include_retracted=body.include_retracted,
            include_superseded=body.include_superseded,
        )

        embedding_status = _embedding_backend_status()

        audit_service.write_event(
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            action="memory_search",
            resource_type="memory_search",
            resource_id=None,
            result="success",
            details={
                "query": body.query,
                "results": len(records),
                "retrieval_mode": retrieval_mode,
                "embedding_backend_status": _embedding_backend_label(embedding_status),
            },
        )

        if retrieval_mode == "fts_only" and _retrieval_is_degraded(embedding_status):
            audit_service.write_event(
                actor_type=ctx.actor_type,
                actor_id=ctx.actor_id,
                action="retrieval_degraded",
                resource_type="memory_search",
                resource_id=None,
                result="success",
                details={
                    "retrieval_mode": retrieval_mode,
                    "embedding_backend_status": _embedding_backend_label(embedding_status),
                    "model_configured": bool(embedding_status.get("model_configured", False)),
                },
            )

        return success_response_with_headers({
            "records": records,
            "retrieval_mode": retrieval_mode,
            "embedding_backend_status": _embedding_backend_label(embedding_status),
            "total": len(records),
        }, rate_headers)
    finally:
        CSG.release(ctx.agent_id)


@router.post("/get")
async def get_memory(
    body: GetMemoryRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    if body.scope:
        if not enforcer.can_read(body.scope):
            return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
        records = memory_service.get_memory_by_scope(
            scope=body.scope,
            limit=min(body.limit, 100),
            offset=body.offset,
            record_status=body.record_status,
        )
    else:
        allowed_scopes = enforcer.filter_readable_scopes(ctx.read_scopes)
        all_records = []
        for scope in allowed_scopes:
            all_records.extend(
                memory_service.get_memory_by_scope(scope, limit=body.limit, offset=0)
            )
        all_records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
        records = all_records[:body.limit]

    return success_response({"records": records, "total": len(records)})


@router.post("/restore")
async def restore_memory(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    record_id = request.query_params.get("record_id")
    if not record_id:
        try:
            body = await request.json()
            if isinstance(body, dict):
                record_id = body.get("record_id")
        except Exception:
            record_id = None
    if not record_id:
        return error_response("MISSING_RECORD_ID", "record_id is required", 400)

    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    success = memory_service.restore_memory(record_id)
    if not success:
        return error_response("NOT_RETRACTED", "Record is not retracted", 400)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_restore",
        resource_type="memory_record",
        resource_id=record_id,
        result="success",
    )

    return success_response({"message": "Memory record restored"})


@router.post("/retract")
async def retract_memory(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    record_id = request.query_params.get("record_id")
    if not record_id:
        try:
            body = await request.json()
            if isinstance(body, dict):
                record_id = body.get("record_id")
        except Exception:
            record_id = None
    if not record_id:
        return error_response("MISSING_RECORD_ID", "record_id is required", 400)

    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    success = memory_service.retract_memory(record_id, retracted_by=ctx.actor_id)
    if not success:
        return error_response("ALREADY_RETRACTED", "Record already retracted or not found", 400)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_retract",
        resource_type="memory_record",
        resource_id=record_id,
        result="success",
    )

    return success_response({"message": "Memory record retracted"})


@router.delete("/{record_id}")
async def delete_memory_record(
    record_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    memory_service.delete_memory_hard(record_id)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_delete",
        resource_type="memory_record",
        resource_id=record_id,
        result="success",
    )

    return success_response({"message": "Memory record permanently deleted"})


@router.get("/{record_id}")
async def get_memory_record(
    record_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    return success_response({"record": record})


@router.get("/{record_id}/chain")
async def get_memory_chain(
    record_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    chain = memory_service.get_supersession_chain(record_id)
    return success_response({"chain": chain})
