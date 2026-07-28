import json
import logging
import re
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response

from app.security.dependencies import get_mcp_request_context
from app.security.scope_enforcer import ScopeEnforcer
from app.security.context import RequestContext
from app.security.pii_detector import contains_pii
from app.branding import APP_NAME, CREDENTIAL_PREFIX, MCP_SERVER_DESCRIPTION
from app.services import (
    memory_service,
    credential_service,
    activity_service,
    briefing_service,
    audit_service,
    tool_spill_service,
    embedding_service,
)
from app.config import settings
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS

logger = logging.getLogger(__name__)



router = APIRouter(prefix="", tags=["mcp"])


MANIFEST = {
    "schema_version": "1.0",
    "name": APP_NAME,
    "version": "1.0.0",
    "description": MCP_SERVER_DESCRIPTION,
    "tools": [
        {
            "name": "memory_search",
            "description": (
                "Search memory records by text query. With no scope, searches your "
                "default recall scopes; pass scope to target one specific readable "
                "scope on demand (e.g. another project)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "scope": {"type": "string"},
                    "topic": {"type": "string"},
                    "memory_class": {"type": "string", "enum": list(MEMORY_CLASSES)},
                    "min_confidence": {"type": "number"},
                    "subject_anchor": {
                        "type": "string",
                        "description": "Only records anchored to this repo path, host, or service — prefix match, so 'repo:app/services' matches everything under it.",
                    },
                    "activity_id": {
                        "type": "string",
                        "description": "Only records written during that activity — what a given piece of work concluded.",
                    },
                    "as_of": {
                        "type": "string",
                        "description": "ISO date/datetime. Answers what was held to be true at that moment rather than now — superseded records come back while their validity window covers it. Use for 'what did we think in March', not for current state.",
                    },
                    "view": {
                        "type": "string",
                        "enum": ["lean", "full"],
                        "description": "Defaults to lean (content plus the fields you can act on). Use full only when you need lifecycle columns such as provenance or supersession.",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "include_retracted": {"type": "boolean", "default": False},
                    "include_superseded": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_get",
            "description": (
                "Get memory records by scope. Defaults to active records only; pass "
                "record_status='all' to also see retracted/superseded records, or a "
                "specific status to inspect just that one. Use "
                "view='compact' to survey/audit a scope (metadata + a short content "
                "preview, no full bodies); then memory_search or "
                "memory_get(view='full', limit=…) for full content. Defaults to "
                "compact for large pages, full for small ones."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "record_status": {
                        "type": "string",
                        "description": "Defaults to 'active'. Use 'all' to include retracted/superseded records, or name a specific status.",
                    },
                    "view": {"type": "string", "enum": ["full", "compact"]},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
            },
        },
        {
            "name": "memory_write",
            "description": (
                "Write a durable memory record — something that will still be worth "
                "knowing in a future session. Reporting what you just did belongs in "
                "activity_update instead."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "memory_class": {
                        "type": "string",
                        "enum": list(MEMORY_CLASSES),
                        "description": (
                            "What would settle this record if someone doubted it? "
                            "'fact' = an observation about how things ARE, checkable "
                            "against code/a host/a service, and false once the world "
                            "changes (e.g. 'the build server is 192.0.2.10'). 'decision' = a "
                            "choice about how things SHOULD be, which only a person can "
                            "revise and nothing can verify (e.g. 'do not edit vendored "
                            "dependencies directly'). 'preference' = a standing user or "
                            "team preference. "
                            "'scratchpad' = a temporary note. Settled by checking -> fact; "
                            "settled by deciding -> decision."
                        ),
                    },
                    "scope": {"type": "string"},
                    "topic": {
                        "type": "string",
                        "description": "Short human-readable label for this record, shown in listings.",
                    },
                    "confidence": {
                        "type": "number",
                        "default": 0.5,
                        "description": "Deprecated. Self-assessment does not vary in practice; freshness is derived from last_confirmed_at instead.",
                    },
                    "importance": {"type": "number", "default": 0.5},
                    "source_kind": {
                        "type": "string",
                        "enum": list(SOURCE_KINDS),
                        "default": "agent_inference",
                    },
                    "subject_anchor": {
                        "type": "string",
                        "description": (
                            "What a later session would look at to check this record, as "
                            "'type:value' — 'repo:app/services/memory_service.py', "
                            "'host:192.0.2.10', or 'service:<binding_id>'. Repo paths must "
                            "be RELATIVE to the workspace root: the same directory has a "
                            "different absolute path in every agent's container, so an "
                            "absolute path is only resolvable by whoever wrote it. Name the "
                            "thing you actually looked at; omit it if nothing could verify "
                            "this (which is normal for a decision)."
                        ),
                    },
                    "supersedes_id": {"type": "string"},
                    "slot_key": {"type": "string"},
                    "valid_from": {"type": "string"},
                    "valid_to": {"type": "string"},
                    "last_confirmed_at": {"type": "string"},
                    "expires_at": {"type": "string", "description": "ISO datetime after which this record is excluded from search results and swept on next maintenance run"},
                },
                "required": ["content", "memory_class", "scope"],
            },
        },
        {
            "name": "memory_pinned",
            "description": (
                "The standing context for your scopes: rules and constraints the operator "
                "wants applied to every session. Call this once at the start of a session — "
                "these are loaded, not searched for, because a constraint that has to win a "
                "search can be missed."
            ),
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "memory_pin",
            "description": (
                "Request that a record become standing context shown to every session, or "
                "that it stop being. Reserved for the few rules that should apply regardless "
                "of the task. This queues the request for an operator: standing context "
                "reaches every session in the scope, so it is granted rather than taken."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "pinned": {"type": "boolean", "default": True},
                },
                "required": ["record_id"],
            },
        },
        {
            "name": "memory_confirm",
            "description": (
                "Mark a record as verified against the world, as of now. Only call this "
                "after actually checking — reading the record is not checking it. Clears "
                "the record's staleness so later sessions can tell a checked fact from an "
                "unchecked one."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "evidence": {
                        "type": "string",
                        "description": "What you looked at, e.g. 'adapter.json reports version 1.0.1' or 'ssh router: /etc/version = v3.0.1.5862409'.",
                    },
                },
                "required": ["record_id", "evidence"],
            },
        },
        {
            "name": "memory_reanchor",
            "description": (
                "Repoint a record at what actually describes it. Use when a verification "
                "check reports a missing anchor but the memory itself is still good — the "
                "file moved, or the anchor was wrong to begin with."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "subject_anchor": {
                        "type": "string",
                        "description": "New anchor as 'repo:<path>', 'host:<name-or-ip>' or 'service:<binding_id>'. Pass an empty string to remove it.",
                    },
                },
                "required": ["record_id", "subject_anchor"],
            },
        },
        {
            "name": "memory_verify",
            "description": (
                "Check anchored facts against the thing they describe — a repo path or a "
                "connector binding — and record evidence on the ones that pass. Records "
                "whose anchor has vanished are queued for review rather than retracted. "
                "Reports verified / missing / unverifiable counts."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Limit to one scope."},
                    "limit": {"type": "integer", "default": 50},
                },
            },
        },
        {
            "name": "memory_feedback",
            "description": (
                "Say whether a recalled record actually helped. Feeds ranking with "
                "observed usefulness instead of the writer's own estimate."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "helpful": {"type": "boolean"},
                },
                "required": ["record_id", "helpful"],
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
            "name": "memory_move",
            "description": (
                "Atomically relocate an active memory record to a new scope: copies "
                "content/class/topic/slot_key into new_scope (stamping moved_from + "
                "supersedes_id lineage) and retracts the original. Requires write "
                "access to BOTH scopes. Returns the new record."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "record_id": {"type": "string"},
                    "new_scope": {"type": "string"},
                    "source_kind": {
                        "type": "string",
                        "enum": list(SOURCE_KINDS),
                        "default": "agent_inference",
                    },
                },
                "required": ["record_id", "new_scope"],
            },
        },
        {
            "name": "credential_get",
            "description": "Get a credential reference name by entry ID",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "string"},
                },
                "required": ["entry_id"],
            },
        },
        {
            "name": "credential_list",
            "description": "List credential references in authorized scopes",
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
                    "task_note": {"type": "string"},
                    "task_result": {"type": "string"},
                    "status": {"type": "string"},
                    "memory_scope": {"type": "string"},
                },
            },
        },
        {
            "name": "activity_get",
            "description": "Get an activity by ID, together with the memory records that were written during it",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string"},
                },
                "required": ["activity_id"],
            },
        },
        {
            "name": "activity_list",
            "description": "List activities visible to the current agent or user",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "agent_id": {"type": "string"},
                    "assigned_agent_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
            },
        },
        {
            "name": "activity_search",
            "description": "Search the activity trail — what agents actually worked on, one record per task, with results. Use this for 'what did we do on X', 'what has been tried', or 'what happened last week' questions; use memory_search for durable facts and decisions. Returns newest-first.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "agent_id": {
                        "type": "string",
                        "description": "Limit to work this agent did or was assigned",
                    },
                    "memory_scope": {
                        "type": "string",
                        "description": "Limit to one workspace/agent scope, e.g. workspace:my-project",
                    },
                    "status": {"type": "string"},
                    "since": {
                        "type": "string",
                        "description": "ISO date/datetime; only activities started on or after it",
                    },
                    "limit": {"type": "integer", "default": 20},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["query"],
            },
        },
        {
            "name": "activity_pickup",
            "description": "Claim the next active work item assigned to this agent in authorized scopes. Call this at startup or when idle to discover work a human has assigned. Returns the claimed activity or null when no work is waiting.",
            "inputSchema": {
                "type": "object",
                "properties": {},
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
            "name": "briefing_list",
            "description": "List generated briefings visible to the current agent or user",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "agent_id": {"type": "string"},
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
            },
        },
        {
            "name": "connectors_list",
            "description": (
                "List installed connector types as lean summaries (id, name, "
                "auth/backend type, action_count) — no full specs. Use "
                "connectors_actions_list for a type's actions, or connectors_summary "
                "for a capability overview."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "limit": {"type": "integer", "default": 50},
                    "offset": {"type": "integer", "default": 0},
                },
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
            "name": "connectors_summary",
            "description": "Summarize visible connector types, bindings, credentials, actions, and health state for the current caller",
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
        {
            "name": "result_fetch",
            "description": (
                "Retrieve a slice of a previously offloaded large tool result. "
                "When a tool's output is too big to return inline, it is offloaded "
                "and you receive a handle + summary instead; call this with that "
                "handle to read the full payload in chunks (use the returned "
                "next_offset to page)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "handle": {"type": "string"},
                    "offset": {"type": "integer", "default": 0},
                    "limit": {"type": "integer", "default": 4000},
                },
                "required": ["handle"],
            },
        },
    ],
}


_REQUIRED_PARAMS = {
    tool["name"]: tuple(tool.get("inputSchema", {}).get("required", []))
    for tool in MANIFEST["tools"]
}

# The schema already explains each parameter. Reusing those descriptions means a
# caller that omits one is told what it is for, not just that it is missing.
_PARAM_DESCRIPTIONS = {
    tool["name"]: {
        name: spec.get("description", "")
        for name, spec in tool.get("inputSchema", {}).get("properties", {}).items()
    }
    for tool in MANIFEST["tools"]
}


def _missing_required_params(tool: str, params: dict) -> list[str]:
    """Required parameters the caller left out, per the published manifest."""
    required = _REQUIRED_PARAMS.get(tool, ())
    return [name for name in required if params.get(name) is None]


def _missing_params_message(tool: str, missing: list[str]) -> str:
    described = _PARAM_DESCRIPTIONS.get(tool, {})
    parts = []
    for name in missing:
        detail = described.get(name, "")
        parts.append(f"{name} ({detail.rstrip('.')})" if detail else name)
    return f"{tool} requires {', '.join(parts)}"


def _mcp_error(code: str, message: str, status: int = 200) -> JSONResponse:
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


def _maybe_offload_tool_result(
    tool_name: str, text: str, is_error: bool, ctx: RequestContext
) -> str:
    """Offload oversized successful tool results to spill storage.

    Returns the original text when it is within budget, otherwise persists the
    full payload and returns a compact summary + retrieval handle. result_fetch
    output is never offloaded (it is the retrieval path and is already chunked).
    """
    threshold = settings.TOOL_RESULT_SPILL_THRESHOLD
    if (
        is_error
        or threshold <= 0
        or tool_name == "result_fetch"
        or len(text) <= threshold
    ):
        return text
    try:
        spill = tool_spill_service.spill(
            agent_id=ctx.agent_id,
            tool=tool_name,
            content=text,
            ttl_hours=settings.TOOL_RESULT_SPILL_TTL_HOURS,
        )
    except Exception:
        # Never fail a tool call because offloading failed; fall back to inline.
        return text
    audit_service.write_event(
        actor_type="agent",
        actor_id=ctx.agent_id,
        action="tool_result_offloaded",
        resource_type="tool_result_spill",
        resource_id=spill["handle"],
        result="success",
        details={"tool": tool_name, "total_chars": spill["total_chars"]},
    )
    return json.dumps(spill, indent=2, default=str)


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





def _memory_provenance(ctx: RequestContext, source_kind: str, scope: str) -> str:
    return memory_service.provenance_for_write(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        channel="mcp",
        route="/mcp",
        source_kind=source_kind,
        scope=scope,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
    )


def _memory_audit_details(record: dict, **extra) -> dict:
    details = {
        "record_id": record.get("id"),
        "memory_class": record.get("memory_class"),
        "scope": record.get("scope"),
    }
    if record.get("topic"):
        details["topic"] = record.get("topic")
    if record.get("slot_key"):
        details["slot_key"] = record.get("slot_key")
    details.update({k: v for k, v in extra.items() if v is not None})
    return details



def _connector_action_count(ct: dict) -> int:
    """Best-effort action count for a connector type without serializing its spec."""
    actions = ct.get("supported_actions")
    if isinstance(actions, list) and actions:
        return len(actions)
    ops = ct.get("operations_json")
    if ops:
        try:
            data = json.loads(ops)
        except (TypeError, ValueError):
            return 0
        if isinstance(data, dict):
            for key in ("operations", "actions", "paths"):
                val = data.get(key)
                if isinstance(val, (list, dict)):
                    return len(val)
        elif isinstance(data, list):
            return len(data)
    return 0


def _connector_summary(ct: dict) -> dict:
    """Lean projection of a connector type for list responses (no spec body)."""
    description = ct.get("description") or ""
    return {
        "connector_type_id": ct.get("id"),
        "display_name": ct.get("display_name"),
        "description": description[:300],
        "provider_type": ct.get("provider_type"),
        "backend_type": ct.get("backend_type"),
        "auth_type": ct.get("auth_type"),
        "action_count": _connector_action_count(ct),
    }


# Number of records past which memory_get defaults to a compact projection
# (full bodies for a large page blow the MCP tool-output token budget).
_MEMORY_COMPACT_THRESHOLD = 25
_MEMORY_PREVIEW_CHARS = 200


def _compact_memory_record(record: dict) -> dict:
    """Compact projection of a memory record: metadata + a short content preview,
    dropping the full content body and verbose provenance_json."""
    content = record.get("content") or ""
    preview = content[:_MEMORY_PREVIEW_CHARS]
    if len(content) > _MEMORY_PREVIEW_CHARS:
        preview += "…"
    return {
        "id": record.get("id"),
        "memory_class": record.get("memory_class"),
        "scope": record.get("scope"),
        "topic": record.get("topic"),
        "slot_key": record.get("slot_key"),
        "record_status": record.get("record_status"),
        "confidence": record.get("confidence"),
        "importance": record.get("importance"),
        "supersedes_id": record.get("supersedes_id"),
        "superseded_by_id": record.get("superseded_by_id"),
        "created_at": record.get("created_at"),
        "content_preview": preview,
    }


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
                    "name": APP_NAME,
                    "version": "1.0.0",
                },
                "instructions": (
                    f"{APP_NAME} provides workspace memory, activity tracking, and credential access. "
                    "At the start of every non-trivial task: call activity_update (status=active, memory_scope=workspace:<your-scope>), "
                    "then run 2-3 memory_search queries for relevant context. "
                    "Send activity_update heartbeats or task_note progress updates every 1-2 minutes while working; mark completed with task_result when done. "
                    f"Use credential_get for {CREDENTIAL_PREFIX}* references — never ask the user for raw secrets. "
                    "If tools appear unavailable, your host may defer MCP schemas — run the host's tool discovery or schema-load step first."
                ),
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
        text = _maybe_offload_tool_result(tool_name, text, is_error, ctx)
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

    missing = _missing_required_params(tool, params)
    if missing:
        # Handlers read required parameters directly, so a caller that omits one
        # used to surface as a KeyError and a 500 with a traceback. The manifest
        # already declares what each tool requires, so check against that rather
        # than adding a guard per handler and forgetting one.
        return _mcp_error("INVALID_PARAMS", _missing_params_message(tool, missing), 400)

    if tool == "result_fetch":
        handle = (params.get("handle") or "").strip()
        if not handle:
            return _mcp_error("INVALID_PARAMS", "handle is required", 400)
        result = tool_spill_service.fetch(
            handle=handle,
                offset=params.get("offset", 0),
            limit=min(max(params.get("limit", 4000), 1), 50000),
            agent_id=ctx.agent_id,
        )
        if result is None:
            return _mcp_error(
                "NOT_FOUND",
                "No offloaded result for that handle (it may have expired).",
                404,
            )
        return JSONResponse(content={"ok": True, "data": result})

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
        search_scope = params.get("scope")
        if search_scope:
            # On-demand: target one specific scope (e.g. another project), gated
            # by full read access — this is how a narrowed agent reaches past its
            # default recall set when the request is explicitly about that scope.
            if not enforcer.can_read(search_scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            allowed = [search_scope]
        else:
            allowed = enforcer.filter_readable_scopes(
                ctx.default_recall_scopes or ctx.read_scopes
            )
        if not allowed:
            embedding_status = embedding_service.safe_backend_status()
            return JSONResponse(
                content={
                    "ok": True,
                    "data": {
                        "records": [],
                        "retrieval_mode": "fts_only",
                        "embedding_backend_status": embedding_service.backend_label(
                            embedding_status
                        ),
                        "total": 0,
                    },
                }
            )
        try:
            records, mode = memory_service.search_memory(
                query=query_text,
                authorized_scopes=allowed,
                topic=params.get("topic"),
                memory_class=memory_class,
                min_confidence=min_confidence,
                limit=min(params.get("limit", 20), 100),
                offset=params.get("offset", 0),
                include_retracted=params.get("include_retracted", False),
                include_superseded=params.get("include_superseded", False),
                subject_anchor=params.get("subject_anchor"),
                activity_id=params.get("activity_id"),
                as_of=params.get("as_of"),
            )
        except ValueError as e:
            return _mcp_error("INVALID_INPUT", str(e), 400)
        # Lean by default: a search result is for deciding what is relevant, and
        # the bookkeeping columns cost more context than they inform. view="full"
        # is there for a caller that genuinely needs the lifecycle fields.
        if params.get("view") != "full":
            records = [memory_service.lean_record(r) for r in records]
        embedding_status = embedding_service.safe_backend_status()
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
                "embedding_backend_status": embedding_service.backend_label(embedding_status),
            },
        )
        if mode == "fts_only" and embedding_service.retrieval_is_degraded(embedding_status):
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="retrieval_degraded",
                resource_type="memory_search",
                resource_id=None,
                result="success",
                details={
                    "retrieval_mode": mode,
                    "embedding_backend_status": embedding_service.backend_label(
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
                    "embedding_backend_status": embedding_service.backend_label(
                        embedding_status
                    ),
                    "total": len(records),
                },
            }
        )

    elif tool == "memory_get":
        # Compact view (lean metadata + content preview) so a whole scope can be
        # surveyed inline without blowing the tool-output budget. In compact view
        # rows are tiny, so allow a higher cap.
        view = params.get("view")
        if view not in (None, "full", "compact"):
            return _mcp_error(
                "INVALID_PARAMS", "view must be 'full' or 'compact'", 400
            )
        if params.get("scope"):
            if not enforcer.can_read(params["scope"]):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            records = memory_service.get_memory_by_scope(
                scope=params["scope"],
                limit=min(params.get("limit", 50), 200),
                offset=params.get("offset", 0),
                record_status=params.get("record_status"),
            )
        else:
            allowed = enforcer.filter_readable_scopes(
                ctx.default_recall_scopes or ctx.read_scopes
            )
            records = memory_service.get_memory_by_scopes(
                scopes=allowed,
                limit=min(params.get("limit", 50), 200),
                offset=params.get("offset", 0),
                record_status=params.get("record_status"),
            )
        if view is None:
            # Auto: compact for large pages, full for small (back-compat for
            # callers reading a handful of records).
            view = (
                "compact" if len(records) > _MEMORY_COMPACT_THRESHOLD else "full"
            )
        out = (
            [_compact_memory_record(r) for r in records]
            if view == "compact"
            else records
        )
        return JSONResponse(
            content={
                "ok": True,
                "data": {"records": out, "total": len(records), "view": view},
            }
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
        try:
            record, pii_flag = memory_service.write_memory(
                content=params["content"],
                memory_class=params["memory_class"],
                scope=scope,
                    topic=params.get("topic"),
                confidence=confidence,
                importance=importance,
                source_kind=source_kind,
                supersedes_id=supersedes_id,
                provenance_json=_memory_provenance(ctx, source_kind, scope),
                subject_anchor=params.get("subject_anchor"),
                slot_key=params.get("slot_key"),
                valid_from=params.get("valid_from"),
                valid_to=params.get("valid_to"),
                last_confirmed_at=params.get("last_confirmed_at"),
                expires_at=params.get("expires_at"),
            )
        except ValueError as e:
            return _mcp_error("INVALID_INPUT", str(e), 400)
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
            details=_memory_audit_details(
                record,
                action="create",
                source_kind=source_kind,
            ),
        )
        # Advisory only, and computed after the write so a slow embedding check
        # can never cost the caller its record.
        payload = {"record": record}
        warnings = memory_service.assess_memory_write(
            content=params["content"],
            scope=scope,
            memory_class=params["memory_class"],
                topic=params.get("topic"),
            exclude_id=record["id"],
                subject_anchor=params.get("subject_anchor"),
        )
        if warnings:
            payload["warnings"] = warnings
        return JSONResponse(
            content={"ok": True, "data": payload}, status_code=201
        )

    elif tool == "memory_pinned":
        scopes = enforcer.filter_readable_scopes(
            ctx.default_recall_scopes or ctx.read_scopes
        )
        records = memory_service.pinned_records(scopes)
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "records": [memory_service.lean_record(r) for r in records],
                    "count": len(records),
                },
            }
        )

    elif tool == "memory_pin":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Record not found", 404)
        if not enforcer.can_write(record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        if record["record_status"] != "active":
            return _mcp_error("NOT_ACTIVE", "Only an active record can be pinned", 400)

        desired = bool(params.get("pinned", True))
        # An agent asking is not an agent deciding. A pinned record enters every
        # future session in the scope — including other agents' sessions —
        # without anyone searching for it, which makes it the most influential
        # thing an agent could write and the obvious target for a bad one. So
        # agents request, and the operator grants, through the same queue that
        # governs every other change to the corpus.
        from app.services import memory_proposal_service

        proposal_id = memory_proposal_service.queue_proposal(
            rule="pin_request",
            action="pin",
            scope=record["scope"],
            target_ids=[params["record_id"]],
            evidence={
                "pin": desired,
                "requested_by": ctx.agent_id or ctx.actor_id,
                "records": [memory_proposal_service._preview(record)],
            },
        )
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_pin_requested",
            resource_type="memory_record",
            resource_id=params["record_id"],
            result="success",
            details={"pin": desired, "scope": record["scope"], "proposal_id": proposal_id},
        )
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "queued": bool(proposal_id),
                    "proposal_id": proposal_id,
                    "message": (
                        "Queued for review. Standing context applies to every session in "
                        "the scope, so an operator grants it."
                        if proposal_id
                        else "Already requested; it is waiting in the review queue."
                    ),
                },
            }
        )

    elif tool == "memory_confirm":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Record not found", 404)
        if not enforcer.can_write(record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        try:
            confirmed = memory_service.confirm_memory(
                params["record_id"],
                evidence=params.get("evidence") or "",
                verified_by=ctx.agent_id or ctx.actor_id,
            )
        except ValueError as e:
            return _mcp_error("EVIDENCE_REQUIRED", str(e), 400)
        if not confirmed:
            return _mcp_error(
                "NOT_ACTIVE", "Only an active record can be confirmed", 400
            )
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_confirmed",
            resource_type="memory_record",
            resource_id=params["record_id"],
            result="success",
            details={"scope": record["scope"], "evidence": params.get("evidence")},
        )
        return JSONResponse(
            content={"ok": True, "data": {"record": memory_service.lean_record(confirmed)}}
        )

    elif tool == "memory_reanchor":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Record not found", 404)
        if not enforcer.can_write(record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        try:
            updated = memory_service.set_subject_anchor(
                params["record_id"],
                params.get("subject_anchor"),
                changed_by=ctx.agent_id or ctx.actor_id,
            )
        except ValueError as e:
            return _mcp_error("INVALID_ANCHOR", str(e), 400)
        if not updated:
            return _mcp_error("NOT_ACTIVE", "Only an active record can be re-anchored", 400)
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_reanchored",
            resource_type="memory_record",
            resource_id=params["record_id"],
            result="success",
            details={
                "from": record.get("subject_anchor"),
                "to": updated.get("subject_anchor"),
            },
        )
        return JSONResponse(
            content={"ok": True, "data": {"record": memory_service.lean_record(updated)}}
        )

    elif tool == "memory_verify":
        from app.services import verification_service

        verify_scope = params.get("scope")
        if verify_scope and not enforcer.can_write(verify_scope):
            # Verification writes confirmation onto records, so it needs the
            # same authority as any other statement about a scope.
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        if not verify_scope:
            writable = enforcer.filter_readable_scopes(list(ctx.write_scopes))
            if not writable:
                return _mcp_error("SCOPE_DENIED", "No writable scope to verify", 403)

        result = verification_service.verify_scope(
            scope=verify_scope, limit=min(int(params.get("limit", 50) or 50), 200)
        )
        # Drop per-record detail for anything that passed: the interesting output
        # is what could not be confirmed.
        result["results"] = [
            r for r in result["results"] if r["status"] != verification_service.VERIFIED
        ]
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_verified",
            resource_type="memory_record",
            result="success",
            details={
                "scope": verify_scope,
                "checked": result["checked"],
                "verified": result["verified"],
                "missing": result["missing"],
            },
        )
        return JSONResponse(content={"ok": True, "data": result})

    elif tool == "memory_feedback":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Record not found", 404)
        # Read access is the bar: judging whether a record helped you is not a
        # mutation of its content, and a reader who cannot rate what it was
        # given is a reader whose experience never reaches the ranking.
        if not enforcer.can_read(record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        helpful = bool(params.get("helpful"))
        updated = memory_service.record_feedback(params["record_id"], helpful)
        if not updated:
            return _mcp_error("NOT_FOUND", "Record not found", 404)
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_feedback",
            resource_type="memory_record",
            resource_id=params["record_id"],
            result="success",
            details={"helpful": helpful, "scope": record["scope"]},
        )
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "record_id": params["record_id"],
                    "helpful_count": updated.get("helpful_count"),
                    "unhelpful_count": updated.get("unhelpful_count"),
                },
            }
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

    elif tool == "memory_move":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Memory record not found", 404)
        new_scope = params["new_scope"]
        # A move both removes from the source and creates in the destination, so
        # the caller must be able to write BOTH scopes.
        if not enforcer.can_write(record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to source scope", 403)
        if not enforcer.can_write(new_scope):
            return _mcp_error(
                "SCOPE_DENIED", "Access denied to destination scope", 403
            )
        source_kind = params.get("source_kind", "agent_inference")
        if source_kind not in SOURCE_KINDS:
            return _mcp_error(
                "INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400
            )
        new_record, err = memory_service.move_memory(
            record_id=params["record_id"],
            new_scope=new_scope,
            provenance_json=_memory_provenance(ctx, source_kind, new_scope),
        )
        if err == "NOT_FOUND":
            return _mcp_error("NOT_FOUND", "Memory record not found", 404)
        if err == "NOT_ACTIVE":
            return _mcp_error("INVALID_STATE", "Only an active record can be moved", 400)
        if err == "SAME_SCOPE":
            return _mcp_error("INVALID_INPUT", "Record is already in that scope", 400)
        if err == "PII_DETECTED":
            return _mcp_error(
                "PII_DETECTED",
                "Content contains PII and cannot be moved to a shared scope",
                422,
            )
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="memory_move",
            resource_type="memory_record",
            resource_id=new_record["id"],
            result="success",
            details={
                "source_record_id": record["id"],
                "moved_from": record["scope"],
                "moved_to": new_record["scope"],
            },
        )
        return JSONResponse(
            content={"ok": True, "data": {"record": new_record}}, status_code=201
        )

    elif tool == "credential_get":
        entry = credential_service.get_credential(params["entry_id"])
        if not entry:
            return _mcp_error("NOT_FOUND", "Credential entry not found", 404)
        if not enforcer.can_read(entry["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="credential_reference",
            resource_type="credential",
            resource_id=entry["id"],
            result="success",
        )
        return JSONResponse(
            content={"ok": True, "data": {"reference_name": entry["reference_name"]}}
        )

    elif tool == "credential_list":
        scope = params.get("scope")
        if scope:
            if not enforcer.can_read(scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            entries = credential_service.list_credentials(
                scope=scope, limit=min(params.get("limit", 50), 100)
            )
        else:
            allowed = enforcer.filter_readable_scopes(ctx.read_scopes)
            all_entries = []
            for s in allowed:
                all_entries.extend(
                    credential_service.list_credentials(scope=s, limit=100)
                )
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
                    task_note=params.get("task_note"),
                    task_result=params.get("task_result"),
                    memory_scope=memory_scope,
                    status=params["status"],
                )
            elif params.get("task_description") or params.get("task_note") or params.get("task_result") or memory_scope:
                activity_service.update_activity(
                    existing["id"],
                    task_description=params.get("task_description"),
                    task_note=params.get("task_note"),
                    task_result=params.get("task_result"),
                    memory_scope=memory_scope,
                )
            else:
                activity_service.heartbeat_activity(existing["id"])
            _updated = activity_service.get_activity(existing["id"]) or existing
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="activity_update",
                resource_type="activity",
                resource_id=existing["id"],
                result="success",
                details=activity_service.audit_details(
                    _updated,
                    action="heartbeat"
                    if not params.get("status")
                    and not params.get("task_description")
                    and not params.get("task_note")
                    and not params.get("task_result")
                    and not memory_scope
                    else "update",
                    previous_status=existing["status"],
                    new_status=params.get("status") or existing["status"],
                    memory_scope=memory_scope or existing.get("memory_scope"),
                ),
            )
            _new_status = params.get("status")
            if (
                not _new_status
                and not params.get("task_description")
                and not params.get("task_note")
                and not params.get("task_result")
                and not memory_scope
            ):
                _event = "activity_heartbeat"
            elif _new_status == "cancelled":
                _event = "activity_cancelled"
            else:
                _event = "activity_updated"
            activity_service.notify(
                _event, _updated, previous_status=existing["status"]
            )
            return JSONResponse(
                content={"ok": True, "data": {"activity": _updated}},
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
                details=activity_service.audit_details(
                    act,
                    action="create",
                    new_status=act.get("status"),
                ),
            )
            activity_service.notify("activity_created", act)
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
        # What the session concluded, scope-filtered for this caller: the task
        # description says what was attempted, these say what came of it.
        produced = memory_service.records_for_activity(
            params["activity_id"],
            authorized_scopes=None
            if ctx.is_admin
            else enforcer.filter_readable_scopes(ctx.read_scopes),
        )
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "activity": activity,
                    "produced_records": [
                        memory_service.lean_record(r) for r in produced
                    ],
                },
            }
        )

    elif tool == "activity_list":
        agent_filter = params.get("agent_id")
        assigned_filter = params.get("assigned_agent_id")
        status = params.get("status")
        limit = min(int(params.get("limit", 50) or 50), 100)
        offset = max(int(params.get("offset", 0) or 0), 0)
        fetch_limit = min(max(limit + offset, 50), 200)
        enforcer = ScopeEnforcer(
            ctx.read_scopes,
            ctx.write_scopes,
            ctx.agent_id,
            is_admin=ctx.is_admin,
            active_workspace_ids=ctx.active_workspace_ids,
        )
        raw_activities = activity_service.list_activities(
            status=status,
            limit=fetch_limit,
            offset=0,
        )
        activities = []
        for activity in raw_activities:
            memory_scope = activity.get("memory_scope") or f"agent:{activity['agent_id']}"
            if not ctx.is_admin and not enforcer.can_read(memory_scope):
                continue
            if agent_filter and activity.get("agent_id") != agent_filter and activity.get("assigned_agent_id") != agent_filter:
                continue
            if assigned_filter and activity.get("assigned_agent_id") != assigned_filter:
                continue
            activities.append(activity)
        activities = activities[offset : offset + limit]
        return JSONResponse(
            content={
                "ok": True,
                "data": {"activities": activities, "count": len(activities)},
            }
        )

    elif tool == "activity_search":
        query = params.get("query") or ""
        limit = min(int(params.get("limit", 20) or 20), 100)
        offset = max(int(params.get("offset", 0) or 0), 0)
        # Scope filtering happens after the query, same as activity_list, so
        # over-fetch to keep a page full once unreadable rows are dropped.
        fetch_limit = min(max((limit + offset) * 3, 50), 300)
        raw_activities = activity_service.search_activities(
            query,
            agent_id=params.get("agent_id"),
            status=params.get("status"),
            memory_scope=params.get("memory_scope"),
            since=params.get("since"),
            limit=fetch_limit,
            offset=0,
        )
        activities = []
        for activity in raw_activities:
            memory_scope = (
                activity.get("memory_scope") or f"agent:{activity['agent_id']}"
            )
            if not ctx.is_admin and not enforcer.can_read(memory_scope):
                continue
            activities.append(activity)
        activities = activities[offset : offset + limit]
        return JSONResponse(
            content={
                "ok": True,
                "data": {"activities": activities, "count": len(activities)},
            }
        )

    elif tool == "activity_pickup":
        authorized_scopes = enforcer.filter_readable_scopes(ctx.read_scopes)
        activity = activity_service.claim_next_activity(ctx.agent_id, authorized_scopes)
        if activity:
            audit_service.write_event(
                actor_type=ctx.actor_type,
                actor_id=ctx.actor_id,
                action="activity_pickup",
                resource_type="activity",
                resource_id=activity["id"],
                result="success",
                details=activity_service.audit_details(activity, action="pickup"),
            )
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "activity": activity,
                    "message": None if activity else "No assigned work found for this agent in authorized scopes",
                },
            }
        )

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

    elif tool == "briefing_list":
        agent_filter = params.get("agent_id")
        limit = min(int(params.get("limit", 50) or 50), 100)
        offset = max(int(params.get("offset", 0) or 0), 0)
        fetch_limit = min(max(limit + offset, 50), 200)
        raw_briefings = briefing_service.list_briefings(
            agent_id=agent_filter if ctx.is_admin else None,
            limit=fetch_limit,
            offset=0,
        )
        enforcer = ScopeEnforcer(
            ctx.read_scopes,
            ctx.write_scopes,
            ctx.agent_id,
            is_admin=ctx.is_admin,
            active_workspace_ids=ctx.active_workspace_ids,
        )
        briefings = []
        for briefing in raw_briefings:
            memory_scope = briefing.get("memory_scope") or f"agent:{briefing.get('agent_id')}"
            if not ctx.is_admin and not enforcer.can_read(memory_scope):
                continue
            if agent_filter and briefing.get("agent_id") != agent_filter and briefing.get("assigned_agent_id") != agent_filter:
                continue
            briefings.append(briefing)
        briefings = briefings[offset : offset + limit]
        return JSONResponse(
            content={
                "ok": True,
                "data": {"briefings": briefings, "count": len(briefings)},
            }
        )

    elif tool == "connectors_list":
        from app.services import connector_service

        types = connector_service.list_connector_types()
        total = len(types)
        try:
            limit = int(params.get("limit", 50) or 50)
        except (TypeError, ValueError):
            limit = 50
        limit = max(1, min(limit, 200))
        try:
            offset = int(params.get("offset", 0) or 0)
        except (TypeError, ValueError):
            offset = 0
        offset = max(0, offset)
        page = types[offset : offset + limit]
        connectors = [_connector_summary(t) for t in page]
        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "connectors": connectors,
                    "total": total,
                    "limit": limit,
                    "offset": offset,
                },
            }
        )

    elif tool == "connectors_bindings_list":
        from app.services import connector_service

        scope = params.get("scope")
        if scope:
            if not enforcer.can_read(scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        # `enabled_only` restricts the listing; it is not a tri-state filter.
        # False means "include disabled bindings too", so the enabled filter must
        # be dropped entirely rather than inverted to enabled=False, which would
        # return ONLY disabled bindings.
        enabled_filter = True if params.get("enabled_only", True) else None
        if ctx.is_admin:
            bindings = connector_service.list_bindings(
                scope=scope,
                connector_type_id=params.get("connector_type_id"),
                enabled=enabled_filter,
            )
        else:
            allowed = enforcer.filter_readable_scopes(ctx.read_scopes)
            effective_scope = scope if (scope and enforcer.can_read(scope)) else None
            if effective_scope:
                bindings = connector_service.list_bindings(
                    scope=effective_scope,
                    connector_type_id=params.get("connector_type_id"),
                    enabled=enabled_filter,
                )
            else:
                all_bindings = []
                for s in allowed:
                    all_bindings.extend(
                        connector_service.list_bindings(
                            scope=s,
                            connector_type_id=params.get("connector_type_id"),
                            enabled=enabled_filter,
                        )
                    )
                all_bindings.sort(key=lambda b: b.get("created_at", ""), reverse=True)
                bindings = all_bindings[: params.get("limit", 50)]
        if ctx.is_admin and scope is None:
            bindings.sort(key=lambda b: b.get("created_at", ""), reverse=True)
            bindings = bindings[: params.get("limit", 50)]
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
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            action="connector_binding_tested",
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
        tool_result = connector_service.generate_connector_type_tools(
            ct,
            disabled_actions=ct.get("disabled_actions") or [],
            include_disabled=bool(params.get("include_disabled", False)),
            query=params.get("query"),
            limit=min(int(params.get("limit", 100) or 100), 200),
            offset=max(int(params.get("offset", 0) or 0), 0),
        )

        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "connector_type_id": ct["id"],
                    "display_name": ct["display_name"],
                    "auth_type": ct["auth_type"],
                    "actions": [tool["action"] for tool in tool_result["tools"]],
                    "tools": tool_result["tools"],
                },
            }
        )

    elif tool == "connectors_summary":
        from app.services import connector_service

        summary = connector_service.build_capability_summary(
            enforcer,
            connector_type_id=params.get("connector_type_id"),
            scope=params.get("scope"),
            enabled_only=bool(params.get("enabled_only", True)),
        )
        return JSONResponse(content={"ok": True, "data": summary})

    elif tool == "connectors_run":
        from app.services import connector_service

        binding = connector_service.get_binding(params["binding_id"])
        if not binding:
            return _mcp_error("NOT_FOUND", "Binding not found", 200)
        if not enforcer.can_read(binding["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to this binding", 200)
        if not binding.get("enabled"):
            return _mcp_error("DISABLED", "Binding is disabled", 200)

        connector_type = connector_service.get_connector_type(
            binding["connector_type_id"]
        )
        if not connector_type:
            return _mcp_error("NOT_FOUND", "Connector type not found", 200)
        action = params["action"]
        if connector_service.action_requires_write(
            connector_type, action
        ) and not enforcer.can_write(binding["scope"]):
            return _mcp_error(
                "SCOPE_DENIED",
                "This action changes state, which needs write access to the binding's scope",
                200,
            )
        result = connector_service.execute_binding_action_with_logging(
            params["binding_id"], action, params.get("params") or {}
        )
        if not result.get("success") and result.get("error_code") == "DISABLED":
            return _mcp_error("DISABLED", "Binding is disabled", 200)
        if not result.get("success") and result.get("error_code") == "DISABLED_ACTION":
            return _mcp_error("DISABLED_ACTION", result["error"], 200)
        if not result.get("success") and result.get("error_code") == "INVALID_ACTION":
            return _mcp_error("INVALID_ACTION", result["error"], 200)
        if not result.get("success") and result.get("error_code") == "NO_CREDENTIAL":
            return _mcp_error("NO_CREDENTIAL", result["error"], 200)
        if not result.get("success") and result.get("error_code") == "RATE_LIMITED":
            return _mcp_error("RATE_LIMITED", result["error"], 200)

        duration_ms = result.get("duration_ms")
        audit_service.write_event(
            actor_type=ctx.actor_type,
            actor_id=ctx.actor_id,
            action="connector_action_executed",
            resource_type="connector_binding",
            resource_id=params["binding_id"],
            result=result.get("success") and "success" or "failure",
            details={
                "connector_type_id": binding["connector_type_id"],
                "action": params["action"],
                "duration_ms": duration_ms,
                "transport": result.get("transport"),
            },
        )
        return JSONResponse(content={"ok": True, "data": result})

    else:
        return _mcp_error("UNKNOWN_TOOL", f"Unknown tool: {tool}", 400)
