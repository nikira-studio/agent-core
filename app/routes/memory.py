import asyncio

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from typing import Annotated, Optional

from app.services import memory_service, audit_service, embedding_service
from app.security.dependencies import get_request_context, require_capability
from app.security.effective_authority import EffectiveAuthority
from app.security.rate_limiter import RL, CSG
from app.security.response_helpers import (
    success_response,
    success_response_with_headers,
    error_response,
    rate_limited_response,
    rate_limit_headers,
)
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS
from app.operations.memory import (
    MemoryOperationError,
    budget_search_response,
    confirm_memory as run_memory_confirm,
    fetch_memory_by_ids,
    validate_search_query,
    validate_source_kind_authority,
    write_memory as run_memory_write,
)


router = APIRouter(
    prefix="/api/memory",
    tags=["memory"],
    dependencies=[Depends(require_capability("memory"))],
)


class WriteMemoryRequest(BaseModel):
    content: str
    memory_class: str
    scope: str
    topic: Optional[str] = None
    confidence: float = 0.5
    importance: float = 0.5
    source_kind: str = "agent_inference"
    subject_anchor: Optional[str] = None
    supersedes_id: Optional[str] = None
    slot_key: Optional[str] = None
    valid_from: Optional[str] = None
    valid_to: Optional[str] = None
    last_confirmed_at: Optional[str] = None
    expires_at: Optional[str] = None
    execution_id: Optional[str] = None


class SearchMemoryRequest(BaseModel):
    query: str
    scope: Optional[str] = None
    topic: Optional[str] = None
    memory_class: Optional[str] = None
    min_confidence: float = 0.0
    subject_anchor: Optional[str] = None
    activity_id: Optional[str] = None
    as_of: Optional[str] = None
    view: Optional[str] = None
    limit: int = 20
    offset: int = 0
    max_response_chars: Optional[Annotated[int, Field(strict=True, gt=0)]] = None
    include_retracted: bool = False
    include_superseded: bool = False


class GetMemoryRequest(BaseModel):
    ids: Optional[list[str]] = None
    view: Optional[str] = None
    scope: Optional[str] = None
    record_status: Optional[str] = None
    limit: int = 50
    offset: int = 0


@router.post("/write")
async def write_memory(
    body: WriteMemoryRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    allowed, info = RL.check("agent", ctx.agent_id, "memory_write")
    if not allowed:
        return rate_limited_response(
            "RATE_LIMITED", "memory_write rate limit exceeded", **info
        )

    rate_headers = rate_limit_headers(**info)
    try:
        payload = await run_memory_write(
            body.model_dump(), ctx, channel="api", route="/api/memory/write"
        )
    except MemoryOperationError as exc:
        return error_response(exc.code, exc.message, exc.status_code)

    return success_response_with_headers(payload, rate_headers, status_code=201)


@router.post("/search")
async def search_memory(
    body: SearchMemoryRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    allowed, info = RL.check("agent", ctx.agent_id, "memory_search")
    if not allowed:
        return rate_limited_response(
            "RATE_LIMITED", "memory_search rate limit exceeded", **info
        )

    rate_headers = rate_limit_headers(**info)

    if not CSG.acquire(ctx.agent_id):
        return error_response("CONCURRENT_LIMIT", "Too many concurrent searches", 429)

    try:
        try:
            validate_search_query(body.query)
        except MemoryOperationError as exc:
            return error_response(exc.code, exc.message, exc.status_code)

        if body.memory_class and body.memory_class not in MEMORY_CLASSES:
            return error_response(
                "INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400
            )

        if not 0.0 <= body.min_confidence <= 1.0:
            return error_response(
                "INVALID_CONFIDENCE", "min_confidence must be between 0.0 and 1.0", 400
            )

        if body.scope:
            if not ctx.can("memory", "read", scope=body.scope):
                return error_response(
                    "SCOPE_DENIED", "Access denied to this scope", 403
                )
            allowed_scopes = [body.scope]
        else:
            candidates = ctx.default_recall_scopes or ctx.read_scopes
            allowed_scopes = ctx.filter_scopes("memory", "read", candidates)
        if not allowed_scopes:
            embedding_status = await asyncio.to_thread(
                embedding_service.safe_backend_status
            )
            try:
                data = budget_search_response(
                    {
                        "records": [],
                        "retrieval_mode": "fts_only",
                        "embedding_backend_status": embedding_service.backend_label(
                            embedding_status
                        ),
                        "total": 0,
                    },
                    body.max_response_chars,
                )
            except MemoryOperationError as exc:
                return error_response(exc.code, exc.message, exc.status_code)
            return success_response_with_headers(data, rate_headers)

        try:
            records, retrieval_mode = await asyncio.to_thread(
                memory_service.search_memory,
                query=body.query,
                authorized_scopes=allowed_scopes,
                topic=body.topic,
                memory_class=body.memory_class,
                min_confidence=body.min_confidence,
                limit=min(body.limit, 100),
                offset=body.offset,
                include_retracted=body.include_retracted,
                include_superseded=body.include_superseded,
                subject_anchor=body.subject_anchor,
                activity_id=body.activity_id,
                as_of=body.as_of,
            )
        except ValueError as e:
            return error_response("INVALID_INPUT", str(e), 400)

        # Lean by default; see MEMORY_RESULT_FIELDS for what a result keeps.
        if body.view != "full":
            records = [memory_service.lean_record(r) for r in records]

        embedding_status = await asyncio.to_thread(
            embedding_service.safe_backend_status
        )

        try:
            data = budget_search_response(
                {
                    "records": records,
                    "retrieval_mode": retrieval_mode,
                    "embedding_backend_status": embedding_service.backend_label(
                        embedding_status
                    ),
                    "total": len(records),
                },
                body.max_response_chars,
            )
        except MemoryOperationError as exc:
            return error_response(exc.code, exc.message, exc.status_code)
        records = data["records"]
        await asyncio.to_thread(
            memory_service.record_recall, [r["id"] for r in records]
        )
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
                "embedding_backend_status": embedding_service.backend_label(
                    embedding_status
                ),
            },
        )

        if retrieval_mode == "fts_only" and embedding_service.retrieval_is_degraded(
            embedding_status
        ):
            audit_service.write_event(
                actor_type=ctx.actor_type,
                actor_id=ctx.actor_id,
                action="retrieval_degraded",
                resource_type="memory_search",
                resource_id=None,
                result="success",
                details={
                    "retrieval_mode": retrieval_mode,
                    "embedding_backend_status": embedding_service.backend_label(
                        embedding_status
                    ),
                    "model_configured": bool(
                        embedding_status.get("model_configured", False)
                    ),
                },
            )

        return success_response_with_headers(data, rate_headers)
    finally:
        CSG.release(ctx.agent_id)


@router.post("/get")
def get_memory(
    body: GetMemoryRequest,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    if body.ids is not None:
        try:
            data = fetch_memory_by_ids(body.ids, ctx, view=body.view, channel="rest")
        except MemoryOperationError as exc:
            return error_response(exc.code, exc.message, exc.status_code)
        return success_response(data)
    if body.scope:
        if not ctx.can("memory", "read", scope=body.scope):
            return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
        records = memory_service.get_memory_by_scope(
            scope=body.scope,
            limit=min(body.limit, 100),
            offset=body.offset,
            record_status=body.record_status,
        )
    else:
        candidates = ctx.default_recall_scopes or ctx.read_scopes
        allowed_scopes = ctx.filter_scopes("memory", "read", candidates)
        records = memory_service.get_memory_by_scopes(
            scopes=allowed_scopes,
            limit=min(body.limit, 100),
            offset=body.offset,
        )

    return success_response({"records": records, "total": len(records)})


@router.post("/restore")
async def restore_memory(
    request: Request,
    ctx: EffectiveAuthority = Depends(get_request_context),
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

    if not ctx.can("memory", "write", scope=record["scope"]):
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
    ctx: EffectiveAuthority = Depends(get_request_context),
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

    if not ctx.can("memory", "write", scope=record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    success = memory_service.retract_memory(record_id)
    if not success:
        return error_response(
            "ALREADY_RETRACTED", "Record already retracted or not found", 400
        )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="memory_retract",
        resource_type="memory_record",
        resource_id=record_id,
        result="success",
    )

    return success_response({"message": "Memory record retracted"})


@router.post("/confirm")
async def confirm_memory(
    request: Request,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    """Mark a record as confirmed against the world, with evidence.

    Mirrors the request shape of `/retract`: the record id can come from a
    query param or from a JSON body, and evidence is read from the JSON body
    only. The shared ``app.operations.memory.confirm_memory`` operation owns
    the evidence-required check, the authorization check, the call into
    ``memory_service.confirm_memory``, and the audit event — this route is
    just a transport.
    """
    record_id = request.query_params.get("record_id")
    evidence: Optional[str] = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            if not record_id:
                record_id = body.get("record_id")
            evidence = body.get("evidence")
    except Exception:
        pass
    if not record_id:
        return error_response("MISSING_RECORD_ID", "record_id is required", 400)

    try:
        payload = await run_memory_confirm(
            record_id,
            evidence=evidence or "",
            authority=ctx,
            channel="api",
            route="/api/memory/confirm",
        )
    except MemoryOperationError as exc:
        return error_response(exc.code, exc.message, exc.status_code)

    return success_response(payload)


@router.post("/move")
async def move_memory(
    request: Request,
    ctx: EffectiveAuthority = Depends(get_request_context),
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

    if not ctx.can("memory", "write", scope=record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to source scope", 403)
    if not ctx.can("memory", "write", scope=new_scope):
        return error_response("SCOPE_DENIED", "Access denied to destination scope", 403)

    # Caller-supplied source_kind on a move goes into the audit provenance for
    # the move itself, not into the moved record's stored tier (which is
    # always preserved server-side from the original). It is still the same
    # claim of authorship and has the same restriction — a genuine human
    # session is the only actor that may assert human provenance.
    denied = validate_source_kind_authority(source_kind, ctx)
    if denied is not None:
        return error_response(denied[0], denied[1], 403)

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
        return error_response(
            "INVALID_STATE", "Only an active record can be moved", 400
        )
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
def delete_memory_record(
    record_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    if not ctx.can("memory", "write", scope=record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    try:
        success = memory_service.delete_memory_hard(record_id)
    except Exception as exc:
        return error_response(
            "DELETE_FAILED", f"Unable to delete memory record: {exc}", 500
        )

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
def get_memory_record(
    record_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    if not ctx.can("memory", "read", scope=record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    return success_response({"record": record})


@router.get("/{record_id}/chain")
def get_memory_chain(
    record_id: str,
    ctx: EffectiveAuthority = Depends(get_request_context),
):
    record = memory_service.get_memory_record(record_id)
    if not record:
        return error_response("NOT_FOUND", "Memory record not found", 404)

    if not ctx.can("memory", "read", scope=record["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)

    chain = memory_service.get_supersession_chain(record_id)
    return success_response({"chain": chain})
