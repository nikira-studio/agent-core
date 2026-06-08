import re

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
from app.security.pii_detector import contains_pii


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


def _memory_provenance(
    ctx: RequestContext,
    source_kind: str,
    scope: str,
) -> str:
    return memory_service.build_provenance(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        channel="api",
        source_kind=source_kind,
        scope=scope,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        extras={"route": "/api/memory/write"},
    )


def _memory_import_provenance(
    ctx: RequestContext,
    scope: str,
    filename: str,
    chunk_index: int,
    chunk_count: int,
) -> str:
    return memory_service.build_provenance(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        channel="api",
        source_kind="external_import",
        scope=scope,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        extras={
            "route": "/api/memory/import",
            "import_source": filename,
            "import_chunk": chunk_index,
            "import_chunks": chunk_count,
        },
    )


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
    slot_key: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    last_confirmed_at: Optional[str] = None
    expires_at: Optional[str] = None


class ImportMemorySource(BaseModel):
    filename: str = "notes.txt"
    content: str


class ImportMemoryRequest(BaseModel):
    scope: str
    sources: list[ImportMemorySource]
    memory_class: str = "fact"
    domain: Optional[str] = "import"
    topic: Optional[str] = None
    confidence: float = 0.85
    importance: float = 0.6


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

    try:
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
            provenance_json=_memory_provenance(ctx, body.source_kind, body.scope),
            slot_key=body.slot_key,
            valid_from=body.valid_from,
            valid_to=body.valid_to,
            last_confirmed_at=body.last_confirmed_at,
            expires_at=body.expires_at,
        )
    except ValueError as e:
        return error_response("INVALID_INPUT", str(e), 400)

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


@router.post("/import")
async def import_memory(
    body: ImportMemoryRequest,
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
    if not 0.0 <= body.confidence <= 1.0:
        return error_response("INVALID_CONFIDENCE", "confidence must be between 0.0 and 1.0", 400)
    if not 0.0 <= body.importance <= 1.0:
        return error_response("INVALID_IMPORTANCE", "importance must be between 0.0 and 1.0", 400)
    if not body.sources:
        return error_response("NO_SOURCES", "At least one import source is required", 400)
    if len(body.sources) > 20:
        return error_response("TOO_MANY_SOURCES", "At most 20 sources can be imported at once", 400)

    parsed_sources = []
    total_chars = 0
    total_chunks = 0
    for source in body.sources:
        total_chars += len(source.content)
        if total_chars > 500_000:
            return error_response("IMPORT_TOO_LARGE", "Combined import content is too large", 413)
        filename = memory_service.sanitize_import_filename(source.filename)
        chunks = memory_service.parse_import_text(source.content, filename)
        if not chunks:
            continue
        total_chunks += len(chunks)
        parsed_sources.append({"filename": filename, "chunks": chunks})

    if not parsed_sources:
        return error_response("EMPTY_IMPORT", "No importable text was found", 400)
    if total_chunks > 250:
        return error_response("TOO_MANY_RECORDS", "Import would create too many memory records", 400)

    if body.scope == "shared":
        for parsed in parsed_sources:
            for chunk in parsed["chunks"]:
                if contains_pii(chunk["content"]):
                    return error_response(
                        "PII_DETECTED",
                        "Imported content contains PII and cannot be written to shared scope",
                        422,
                    )

    imported = []
    records = []
    try:
        for parsed in parsed_sources:
            record_ids = []
            chunks = parsed["chunks"]
            for index, chunk in enumerate(chunks, start=1):
                record, pii_flag = memory_service.write_memory(
                    content=chunk["content"],
                    memory_class=body.memory_class,
                    scope=body.scope,
                    domain=body.domain,
                    topic=body.topic or parsed["filename"],
                    confidence=body.confidence,
                    importance=body.importance,
                    source_kind="external_import",
                    provenance_json=_memory_import_provenance(
                        ctx, body.scope, parsed["filename"], index, len(chunks)
                    ),
                )
                if pii_flag == "PII_DETECTED":
                    return error_response(
                        "PII_DETECTED",
                        "Imported content contains PII and cannot be written to shared scope",
                        422,
                    )
                records.append(record)
                record_ids.append(record["id"])
            imported.append(
                {
                    "filename": parsed["filename"],
                    "record_count": len(record_ids),
                    "record_ids": record_ids,
                }
            )
    except ValueError as e:
        return error_response("INVALID_INPUT", str(e), 400)

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_import",
        resource_type="memory_record",
        resource_id=None,
        result="success",
        details={
            "scope": body.scope,
            "source_count": len(imported),
            "record_count": len(records),
        },
    )

    return success_response_with_headers(
        {"imported": imported, "records": records, "total_records": len(records)},
        rate_headers,
        status_code=201,
    )


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
            allowed_scopes = enforcer.filter_readable_scopes(
                ctx.default_recall_scopes or ctx.read_scopes
            )
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
        allowed_scopes = enforcer.filter_readable_scopes(
            ctx.default_recall_scopes or ctx.read_scopes
        )
        records = memory_service.get_memory_by_scopes(
            scopes=allowed_scopes,
            limit=min(body.limit, 100),
            offset=body.offset,
        )

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

    success = memory_service.retract_memory(record_id)
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


@router.post("/move")
async def move_memory(
    request: Request,
    ctx: RequestContext = Depends(get_request_context),
):
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    record_id = request.query_params.get("record_id") or body.get("record_id")
    new_scope = request.query_params.get("new_scope") or body.get("new_scope")
    source_kind = body.get("source_kind") or "agent_inference"
    if not record_id:
        return error_response("MISSING_RECORD_ID", "record_id is required", 400)
    if not new_scope:
        return error_response("MISSING_NEW_SCOPE", "new_scope is required", 400)
    if source_kind not in SOURCE_KINDS:
        return error_response(
            "INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400
        )

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
        return error_response("SCOPE_DENIED", "Access denied to source scope", 403)
    if not enforcer.can_write(new_scope):
        return error_response("SCOPE_DENIED", "Access denied to destination scope", 403)

    new_record, err = memory_service.move_memory(
        record_id=record_id,
        new_scope=new_scope,
        provenance_json=memory_service.build_provenance(
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            channel="api",
            source_kind=source_kind,
            scope=new_scope,
            user_id=ctx.user_id,
            agent_id=ctx.agent_id,
            extras={"route": "/api/memory/move"},
        ),
    )
    if err == "NOT_FOUND":
        return error_response("NOT_FOUND", "Memory record not found", 404)
    if err == "NOT_ACTIVE":
        return error_response("INVALID_STATE", "Only an active record can be moved", 400)
    if err == "SAME_SCOPE":
        return error_response("INVALID_INPUT", "Record is already in that scope", 400)
    if err == "PII_DETECTED":
        return error_response(
            "PII_DETECTED",
            "Content contains PII and cannot be moved to a shared scope",
            422,
        )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_move",
        resource_type="memory_record",
        resource_id=new_record["id"],
        result="success",
        details={
            "source_record_id": record_id,
            "moved_from": record["scope"],
            "moved_to": new_record["scope"],
        },
    )
    return success_response({"record": new_record}, status_code=201)


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

    try:
        success = memory_service.delete_memory_hard(record_id)
    except Exception as exc:
        return error_response("DELETE_FAILED", f"Unable to delete memory record: {exc}", 500)

    if not success:
        return error_response("NOT_FOUND", "Memory record not found", 404)

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
