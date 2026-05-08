import json
import re
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.security.dependencies import get_request_context, get_mcp_request_context
from app.security.scope_enforcer import ScopeEnforcer
from app.security.context import RequestContext
from app.security.response_helpers import success_response, error_response
from app.security.pii_detector import contains_pii
from app.services import (
    memory_service,
    vault_service,
    activity_service,
    briefing_service,
    audit_service,
)
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS


router = APIRouter(prefix="", tags=["mcp"])


MANIFEST = {
    "schema_version": "1.0",
    "name": "Agent Core",
    "version": "1.0.0",
    "description": "Agent Core local-first AI agent control layer",
    "tools": [
        {
            "name": "memory_search",
            "description": "Search memory records by text query within authorized scopes",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "domain": {"type": "string"},
                    "topic": {"type": "string"},
                    "memory_class": {"type": "string", "enum": list(MEMORY_CLASSES)},
                    "min_confidence": {"type": "number"},
                    "limit": {"type": "integer", "default": 20},
                    "include_retracted": {"type": "boolean", "default": False},
                    "include_superseded": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_get",
            "description": "Get memory records by scope or list active records",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "record_status": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "memory_write",
            "description": "Write a new memory record",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "memory_class": {"type": "string", "enum": list(MEMORY_CLASSES)},
                    "scope": {"type": "string"},
                    "domain": {"type": "string"},
                    "topic": {"type": "string"},
                    "confidence": {"type": "number", "default": 0.5},
                    "importance": {"type": "number", "default": 0.5},
                    "source_kind": {
                        "type": "string",
                        "enum": list(SOURCE_KINDS),
                        "default": "agent_inference",
                    },
                    "supersedes_id": {"type": "string"},
                },
                "required": ["content", "memory_class", "scope"],
            },
        },
        {
            "name": "memory_retract",
            "description": "Retract a memory record by ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                },
                "required": ["record_id"],
            },
        },
        {
            "name": "vault_get",
            "description": "Get a vault credential reference name by entry ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                },
                "required": ["entry_id"],
            },
        },
        {
            "name": "vault_list",
            "description": "List vault credential references in authorized scopes",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "activity_update",
            "description": "Update the current agent's active activity or create one if none exists",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_description": {"type": "string"},
                    "status": {"type": "string"},
                    "memory_scope": {"type": "string"},
                },
            },
        },
        {
            "name": "activity_get",
            "description": "Get a specific activity by ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string"},
                },
                "required": ["activity_id"],
            },
        },
        {
            "name": "get_briefing",
            "description": "Get a handoff briefing by ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "briefing_id": {"type": "string"},
                },
                "required": ["briefing_id"],
            },
        },
        {
            "name": "connectors_list",
            "description": "List available connector types",
            "inputSchema": {
                "type": "object",
                "properties": {},
            },
        },
        {
            "name": "connectors_bindings_list",
            "description": "List connector bindings in authorized scopes",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "connector_type_id": {"type": "string"},
                    "enabled_only": {"type": "boolean", "default": True},
                },
            },
        },
        {
            "name": "connectors_bindings_test",
            "description": "Test a connector binding by resolving the credential and calling the connector's test_connection",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "binding_id": {"type": "string"},
                },
                "required": ["binding_id"],
            },
        },
        {
            "name": "connectors_actions_list",
            "description": "List actions available for a connector type",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector_type_id": {"type": "string"},
                },
                "required": ["connector_type_id"],
            },
        },
        {
            "name": "connectors_run",
            "description": "Run a connector action server-side using a stored credential; the raw secret is never exposed to the agent",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "binding_id": {"type": "string"},
                    "action": {"type": "string"},
                    "params": {"type": "object"},
                },
                "required": ["binding_id", "action"],
            },
        },
    ],
}


def _mcp_error(code: str, message: str, status: int = 400) -> JSONResponse:
    return JSONResponse(
        content={"ok": False, "error": {"code": code, "message": message}},
        status_code=status,
    )


def _jsonrpc_response(
    request_id, result=None, error=None, status: int = 200
) -> JSONResponse:
    payload = {"jsonrpc": "2.0", "id": request_id}
    if error is not None:
        payload["error"] = error
    else:
        payload["result"] = result or {}
    return JSONResponse(content=payload, status_code=status)


def _jsonrpc_error(
    request_id, code: int, message: str, status: int = 200, data=None
) -> JSONResponse:
    error = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return _jsonrpc_response(request_id, error=error, status=status)


def _is_jsonrpc_request(body: dict) -> bool:
    return body.get("jsonrpc") == "2.0" and "method" in body


def _mcp_tool_result_from_custom_response(response: JSONResponse) -> JSONResponse:
    try:
        payload = json.loads(response.body.decode("utf-8"))
    except Exception:
        payload = {
            "ok": False,
            "error": {"message": "Tool returned an invalid response"},
        }
    return payload


def _query_noise_free(query: str) -> bool:
    q = query.strip()
    if len(q) <= 2:
        return False
    trivial = [
        r"^(the|a|an|is|are|was|were|i|you|he|she|it|we|they)\s*$",
        r"^[.,;:!?]+$",
    ]
    for p in trivial:
        if re.match(p, q, re.IGNORECASE):
            return False
    if contains_pii(q):
        return False
    return True


def _embedding_backend_status() -> dict:
    try:
        from app.services import embedding_service

        return embedding_service.get_embedding_backend_status()
    except Exception:
        return {"backend": "unavailable", "model_configured": False}


def _embedding_backend_label(status: dict) -> str:
    return status.get("backend", "unknown")


def _retrieval_is_degraded(status: dict) -> bool:
    return status.get("backend") != "healthy" or not status.get(
        "model_configured", False
    )


@router.get("/mcp")
async def get_mcp_manifest(ctx: RequestContext = Depends(get_mcp_request_context)):
    return JSONResponse(content=MANIFEST)


@router.post("/mcp")
async def handle_mcp_tool(
    request: Request,
    ctx: RequestContext = Depends(get_mcp_request_context),
):
    try:
        body = await request.json()
    except Exception:
        return _mcp_error("INVALID_REQUEST", "Request body must be valid JSON", 400)

    if _is_jsonrpc_request(body):
        return await _handle_mcp_jsonrpc(body, request, ctx)

    return await _handle_custom_mcp_tool(body, ctx)


async def _handle_mcp_jsonrpc(body: dict, request: Request, ctx: RequestContext):
    request_id = body.get("id")
    method = body.get("method")

    if method == "initialize":
        return _jsonrpc_response(
            request_id,
            {
                "protocolVersion": "2024-11-05",
                "capabilities": {
                    "tools": {},
                },
                "serverInfo": {
                    "name": "Agent Core",
                    "version": "1.0.0",
                },
            },
        )

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "ping":
        return _jsonrpc_response(request_id, {})

    if method == "tools/list":
        return _jsonrpc_response(request_id, {"tools": MANIFEST["tools"]})

    if method == "tools/call":
        params = body.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments") or {}
        if not tool_name:
            return _jsonrpc_error(request_id, -32602, "Tool name is required")

        custom_response = await _handle_custom_mcp_tool(
            {"tool": tool_name, "params": arguments}, ctx
        )
        payload = _mcp_tool_result_from_custom_response(custom_response)
        is_error = custom_response.status_code >= 400 or not payload.get("ok", False)
        text = json.dumps(
            payload.get("data") if payload.get("ok") else payload.get("error", payload),
            indent=2,
            default=str,
        )
        return _jsonrpc_response(
            request_id,
            {
                "content": [{"type": "text", "text": text}],
                "isError": is_error,
            },
        )

    return _jsonrpc_error(request_id, -32601, f"Method not found: {method}")


async def _handle_custom_mcp_tool(body: dict, ctx: RequestContext):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    tool = body.get("tool")
    params = body.get("params", {})

    if not tool:
        return _mcp_error("TOOL_REQUIRED", "tool name is required", 400)

    if tool == "memory_search":
        query_text = params.get("query", "").strip()
        if not _query_noise_free(query_text):
            return _mcp_error(
                "QUERY_NOISE",
                "Query is too trivial or contains credential-like pattern",
                400,
            )
        memory_class = params.get("memory_class")
        if memory_class and memory_class not in MEMORY_CLASSES:
            return _mcp_error(
                "INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400
            )
        min_confidence = params.get("min_confidence", 0.0)
        if not 0.0 <= min_confidence <= 1.0:
            return _mcp_error(
                "INVALID_CONFIDENCE", "min_confidence must be between 0.0 and 1.0", 400
            )
        allowed = enforcer.filter_readable_scopes(ctx.read_scopes)
        if not allowed:
            embedding_status = _embedding_backend_status()
            return JSONResponse(
                content={
                    "ok": True,
                    "data": {
                        "records": [],
                        "retrieval_mode": "fts_only",
                        "embedding_backend_status": _embedding_backend_label(
                            embedding_status
                        ),
                        "total": 0,
                    },
                }
            )
        records, mode = memory_service.search_memory(
            query=query_text,
            authorized_scopes=allowed,
            domain=params.get("domain"),
            topic=params.get("topic"),
            memory_class=memory_class,
            min_confidence=min_confidence,
            limit=min(params.get("limit", 20), 100),
            offset=params.get("offset", 0),
            include_retracted=params.get("include_retracted", False),
            include_superseded=params.get("include_superseded", False),
        )
        embedding_status = _embedding_backend_status()
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_search",
            resource_type="memory_search",
            resource_id=None,
            result="success",
            details={
                "query": query_text,
                "results": len(records),
                "retrieval_mode": mode,
                "embedding_backend_status": _embedding_backend_label(embedding_status),
            },
        )
        if mode == "fts_only" and _retrieval_is_degraded(embedding_status):
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="retrieval_degraded",
                resource_type="memory_search",
                resource_id=None,
                result="success",
                details={
                    "retrieval_mode": mode,
                    "embedding_backend_status": _embedding_backend_label(
                        embedding_status
                    ),
                    "model_configured": bool(
                        embedding_status.get("model_configured", False)
                    ),
                },
            )
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "records": records,
                    "retrieval_mode": mode,
                    "embedding_backend_status": _embedding_backend_label(
                        embedding_status
                    ),
                    "total": len(records),
                },
            }
        )

    elif tool == "memory_get":
        if params.get("scope"):
            if not enforcer.can_read(params["scope"]):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            records = memory_service.get_memory_by_scope(
                scope=params["scope"],
                limit=min(params.get("limit", 50), 100),
                offset=params.get("offset", 0),
                record_status=params.get("record_status"),
            )
        else:
            allowed = enforcer.filter_readable_scopes(ctx.read_scopes)
            all_records = []
            for scope in allowed:
                all_records.extend(
                    memory_service.get_memory_by_scope(scope, limit=50, offset=0)
                )
            all_records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
            records = all_records[: params.get("limit", 50)]
        return JSONResponse(
            content={"ok": True, "data": {"records": records, "total": len(records)}}
        )

    elif tool == "memory_write":
        scope = params["scope"]
        if not enforcer.can_write(scope):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        if params["memory_class"] not in MEMORY_CLASSES:
            return _mcp_error(
                "INVALID_CLASS", f"memory_class must be one of {MEMORY_CLASSES}", 400
            )
        source_kind = params.get("source_kind", "agent_inference")
        if source_kind not in SOURCE_KINDS:
            return _mcp_error(
                "INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400
            )
        confidence = params.get("confidence", 0.5)
        importance = params.get("importance", 0.5)
        if not 0.0 <= confidence <= 1.0:
            return _mcp_error(
                "INVALID_CONFIDENCE", "confidence must be between 0.0 and 1.0", 400
            )
        if not 0.0 <= importance <= 1.0:
            return _mcp_error(
                "INVALID_IMPORTANCE", "importance must be between 0.0 and 1.0", 400
            )
        supersedes_id = params.get("supersedes_id")
        if supersedes_id:
            old = memory_service.get_memory_record(supersedes_id)
            if not old:
                return _mcp_error("NOT_FOUND", "Record to supersede not found", 404)
            if old["record_status"] != "active":
                return _mcp_error(
                    "INVALID_SUPERSESSION", "Cannot supersede non-active record", 400
                )
            if not enforcer.can_write(old["scope"]):
                return _mcp_error(
                    "SCOPE_DENIED",
                    "Access denied to scope of record being superseded",
                    403,
                )
        record, pii_flag = memory_service.write_memory(
            content=params["content"],
            memory_class=params["memory_class"],
            scope=scope,
            domain=params.get("domain"),
            topic=params.get("topic"),
            confidence=confidence,
            importance=importance,
            source_kind=source_kind,
            supersedes_id=supersedes_id,
        )
        if pii_flag == "PII_DETECTED":
            return _mcp_error(
                "PII_DETECTED",
                "Content contains PII and cannot be written to shared scope",
                422,
            )
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_write",
            resource_type="memory_record",
            resource_id=record["id"],
            result="success",
        )
        return JSONResponse(
            content={"ok": True, "data": {"record": record}}, status_code=201
        )

    elif tool == "memory_retract":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Memory record not found", 404)
        if not enforcer.can_write(record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        memory_service.retract_memory(params["record_id"])
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_retract",
            resource_type="memory_record",
            resource_id=params["record_id"],
            result="success",
        )
        return JSONResponse(
            content={"ok": True, "data": {"message": "Memory record retracted"}}
        )

    elif tool == "vault_get":
        entry = vault_service.get_vault_entry(params["entry_id"])
        if not entry:
            return _mcp_error("NOT_FOUND", "Vault entry not found", 404)
        if not enforcer.can_read(entry["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="vault_reference",
            resource_type="vault_entry",
            resource_id=entry["id"],
            result="success",
        )
        return JSONResponse(
            content={"ok": True, "data": {"reference_name": entry["reference_name"]}}
        )

    elif tool == "vault_list":
        scope = params.get("scope")
        if scope:
            if not enforcer.can_read(scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            entries = vault_service.list_vault_entries(
                scope=scope, limit=min(params.get("limit", 50), 100)
            )
        else:
            allowed = enforcer.filter_readable_scopes(ctx.read_scopes)
            all_entries = []
            for s in allowed:
                all_entries.extend(vault_service.list_vault_entries(scope=s, limit=100))
            all_entries.sort(key=lambda e: e.get("created_at", ""), reverse=True)
            entries = all_entries[: params.get("limit", 50)]
        return JSONResponse(
            content={"ok": True, "data": {"entries": entries, "total": len(entries)}}
        )

    elif tool == "activity_update":
        existing = activity_service.get_active_activity_for_agent(
            ctx.agent_id, ctx.user_id
        )
        if existing:
            memory_scope = params.get("memory_scope")
            if memory_scope and not enforcer.can_write(memory_scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to memory_scope", 403)
            if params.get("status"):
                if params["status"] in (
                    "completed",
                    "cancelled",
                    "blocked",
                ) and existing["status"] not in ("active", "stale"):
                    return _mcp_error(
                        "INVALID_TRANSITION", "Cannot close a non-active activity", 400
                    )
                activity_service.update_activity(
                    existing["id"],
                    task_description=params.get("task_description"),
                    memory_scope=memory_scope,
                    status=params["status"],
                )
            elif params.get("task_description") or memory_scope:
                activity_service.update_activity(
                    existing["id"],
                    task_description=params.get("task_description"),
                    memory_scope=memory_scope,
                )
            else:
                activity_service.heartbeat_activity(existing["id"])
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="activity_update",
                resource_type="activity",
                resource_id=existing["id"],
                result="success",
            )
            return JSONResponse(
                content={
                    "ok": True,
                    "data": {"activity": activity_service.get_activity(existing["id"])},
                }
            )
        else:
            if not params.get("task_description"):
                return _mcp_error(
                    "TASK_REQUIRED", "task_description required to create activity", 400
                )
            memory_scope = params.get("memory_scope") or f"agent:{ctx.agent_id}"
            if not enforcer.can_write(memory_scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to memory_scope", 403)
            act = activity_service.create_activity(
                agent_id=ctx.agent_id,
                user_id=ctx.user_id or "",
                task_description=params["task_description"],
                memory_scope=memory_scope,
            )
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="activity_update",
                resource_type="activity",
                resource_id=act["id"],
                result="success",
            )
            return JSONResponse(
                content={"ok": True, "data": {"activity": act}}, status_code=201
            )

    elif tool == "activity_get":
        activity = activity_service.get_activity(params["activity_id"])
        if not activity:
            return _mcp_error("NOT_FOUND", "Activity not found", 404)
        if (
            activity.get("agent_id") != ctx.agent_id
            and activity.get("assigned_agent_id") != ctx.agent_id
            and not ctx.is_admin
        ):
            return _mcp_error("FORBIDDEN", "Access denied", 403)
        return JSONResponse(content={"ok": True, "data": {"activity": activity}})

    elif tool == "get_briefing":
        briefing = briefing_service.get_briefing(params["briefing_id"])
        if not briefing:
            return _mcp_error("NOT_FOUND", "Briefing not found", 404)
        act = activity_service.get_activity(params["briefing_id"])
        if (
            act
            and act.get("agent_id") != ctx.agent_id
            and act.get("assigned_agent_id") != ctx.agent_id
            and not ctx.is_admin
        ):
            return _mcp_error("FORBIDDEN", "Access denied", 403)
        return JSONResponse(content={"ok": True, "data": {"briefing": briefing}})

    elif tool == "connectors_list":
        from app.services import connector_service

        types = connector_service.list_connector_types()
        return JSONResponse(content={"ok": True, "data": {"connectors": types}})

    elif tool == "connectors_bindings_list":
        from app.services import connector_service

        scope = params.get("scope")
        if scope:
            if not enforcer.can_read(scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        allowed = enforcer.filter_readable_scopes(ctx.read_scopes)
        effective_scope = scope if (scope and enforcer.can_read(scope)) else None
        if effective_scope:
            bindings = connector_service.list_bindings(
                scope=effective_scope,
                connector_type_id=params.get("connector_type_id"),
                enabled=params.get("enabled_only", True)
                if params.get("enabled_only") is not None
                else True,
            )
        else:
            all_bindings = []
            for s in allowed:
                all_bindings.extend(
                    connector_service.list_bindings(
                        scope=s,
                        connector_type_id=params.get("connector_type_id"),
                        enabled=params.get("enabled_only", True)
                        if params.get("enabled_only") is not None
                        else True,
                    )
                )
            all_bindings.sort(key=lambda b: b.get("created_at", ""), reverse=True)
            bindings = all_bindings[: params.get("limit", 50)]
        return JSONResponse(
            content={"ok": True, "data": {"bindings": bindings, "total": len(bindings)}}
        )

    elif tool == "connectors_bindings_test":
        from app.services import connector_service

        binding = connector_service.get_binding(params["binding_id"])
        if not binding:
            return _mcp_error("NOT_FOUND", "Binding not found", 404)
        if not enforcer.can_read(binding["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this binding", 403)
        result = connector_service.test_binding(params["binding_id"])
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="connector_test",
            resource_type="connector_binding",
            resource_id=params["binding_id"],
            result=result.get("success") and "success" or "failure",
            details={"connector_type_id": binding["connector_type_id"]},
        )
        return JSONResponse(content={"ok": True, "data": result})

    elif tool == "connectors_actions_list":
        from app.services import connector_service

        ct = connector_service.get_connector_type(params["connector_type_id"])
        if not ct:
            return _mcp_error("NOT_FOUND", "Connector type not found", 404)
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "connector_type_id": ct["id"],
                    "display_name": ct["display_name"],
                    "actions": ct["supported_actions"],
                },
            }
        )

    elif tool == "connectors_run":
        from app.services import connector_service
        import time

        binding = connector_service.get_binding(params["binding_id"])
        if not binding:
            return _mcp_error("NOT_FOUND", "Binding not found", 404)
        if not enforcer.can_read(binding["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this binding", 403)
        if not binding.get("enabled"):
            return _mcp_error("DISABLED", "Binding is disabled", 400)

        binding_with_cred = connector_service.get_binding_with_credential(
            params["binding_id"]
        )
        credential = binding_with_cred.get("credential_plaintext")
        if not credential:
            return _mcp_error(
                "NO_CREDENTIAL", "No credential linked to this binding", 400
            )

        connector_type = connector_service.get_connector_type(
            binding["connector_type_id"]
        )
        if not connector_type:
            return _mcp_error("NOT_FOUND", "Connector type not found", 404)

        if params["action"] not in connector_type["supported_actions"]:
            return _mcp_error(
                "INVALID_ACTION", f"Action not supported: {params['action']}", 400
            )

        start = time.time()
        try:
            from app.connectors import get_connector

            connector = get_connector(connector_type["id"])
            if not connector:
                return _mcp_error("NOT_FOUND", "Connector handler not found", 404)
            result = connector.execute(
                action=params["action"],
                params=params.get("params") or {},
                credential=credential,
                config_json=binding.get("config_json"),
            )
        except Exception as e:
            result = {"success": False, "error": str(e)}
        duration_ms = int((time.time() - start) * 1000)

        connector_service.log_execution(
            binding_id=params["binding_id"],
            action=params["action"],
            params_json=json.dumps(params.get("params")),
            result_status="success" if result.get("success") else "failure",
            result_body_json=json.dumps(result) if result.get("success") else None,
            error_message=result.get("error") if not result.get("success") else None,
            duration_ms=duration_ms,
        )
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="connector_run",
            resource_type="connector_binding",
            resource_id=params["binding_id"],
            result=result.get("success") and "success" or "failure",
            details={
                "connector_type_id": binding["connector_type_id"],
                "action": params["action"],
                "duration_ms": duration_ms,
            },
        )
        return JSONResponse(content={"ok": True, "data": result})

    else:
        return _mcp_error("UNKNOWN_TOOL", f"Unknown tool: {tool}", 400)
