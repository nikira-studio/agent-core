import asyncio
import json
import time
import logging
from typing import Optional

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, Response
from jsonschema import Draft202012Validator

from app.security.dependencies import get_mcp_request_context
from app.security.rate_limiter import CSG, RL
from app.security.response_helpers import rate_limited_response
from app.security.scope_enforcer import ScopeEnforcer
from app.security.effective_authority import EffectiveAuthority
from app.branding import APP_NAME, CREDENTIAL_PREFIX, MCP_SERVER_DESCRIPTION
from app.services import (
    memory_service,
    credential_service,
    activity_service,
    audit_service,
    tool_spill_service,
    artifact_export_service,
    embedding_service,
)
from app.config import settings
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS
from app.operations.connector_actions import run_connector_action
from app.operations.memory import (
    MemoryOperationError,
    budget_search_response,
    confirm_memory as run_memory_confirm,
    fetch_memory_by_ids,
    validate_search_query,
    validate_response_budget,
    validate_source_kind_authority,
    write_memory as run_memory_write,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="", tags=["mcp"])

# Keep tool discovery and execution on one closed policy table. A tool omitted
# here is a release error, not an implicitly public endpoint.
_TOOL_CAPABILITIES = {
    **{
        name: "memory"
        for name in (
            "memory_search",
            "memory_get",
            "memory_write",
            "memory_pin",
            "memory_confirm",
            "memory_reanchor",
            "memory_verify",
            "memory_feedback",
            "memory_retract",
            "memory_move",
        )
    },
    **{
        name: "coordination"
        for name in (
            "memory_pinned",
            "activity_update",
            "activity_get",
            "activity_list",
            "activity_search",
            "activity_pickup",
            "activity_handoff_resolve",
            "attention_list",
            "workspace_sync",
            "workspace_sync_ack",
            "session_context",
        )
    },
    **{name: "credentials" for name in ("credential_get", "credential_list", "credential_request")},
    **{
        name: "connectors_read"
        for name in (
            "connectors_list",
            "connectors_resolve",
            "connectors_bindings_list",
            "connectors_bindings_test",
            "connectors_actions_list",
            "connectors_summary",
        )
    },
    "connectors_run": "connectors_execute",
}


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
                    "offset": {"type": "integer", "default": 0},
                    "max_response_chars": {
                        "type": ["integer", "null"], "minimum": 1,
                        "description": "Optional limit on the pretty-printed JSON data response, including metadata. Returns whole records in rank order. When omitted, the response is trimmed to fit inline; if response_limited is true, page on by raising offset by the number of records returned.",
                    },
                    "include_retracted": {"type": "boolean", "default": False},
                    "include_superseded": {"type": "boolean", "default": False},
                },
                "required": ["query"],
            },
        },
        {
            "name": "memory_get",
            "description": (
                "Get memory records by id (ids=[...], as listed in the session context "
                "index), or by scope. By scope, defaults to active records only; pass "
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
                    "ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "maxItems": 50,
                        "description": "Fetch these records in full. When given, scope and paging are ignored.",
                    },
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
                    "expires_at": {
                        "type": "string",
                        "description": "ISO datetime after which this record is excluded from search results and swept on next maintenance run",
                    },
                    "execution_id": {
                        "type": "string",
                        "description": "Execution returned by workspace_sync for source attribution",
                    },
                },
                "required": ["content", "memory_class", "scope"],
            },
        },
        {
            "name": "memory_pinned",
            "description": (
                "Standing context for your authorized scopes. workspace_sync already returns "
                "pinned records for its workspace. Call memory_pinned at session start only "
                "when you also need standing rules from other authorized scopes or no "
                "workspace sync applies. Pinned records are loaded, not searched for."
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
                "connector binding. A present anchor sets anchor_checked_at; it does not "
                "confirm the record, which needs memory_confirm with evidence. Records "
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
            "name": "credential_request",
            "description": (
                "Ask a person for a secret you need: a token, password or key that is not stored yet. "
                "Never ask for the raw value in the conversation. This opens a request the owner fills "
                "on the Agent Core Credentials page; the value never passes through you. Call it again "
                "with the same scope and name to check: once filled it returns the AC_SECRET reference."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string", "description": "Where to store it, e.g. workspace:<id> or user:<id>."},
                    "name": {"type": "string", "description": "Short stable name, e.g. github-token."},
                    "purpose": {"type": "string", "description": "What it is for and where to get it, shown to the person."},
                },
                "required": ["scope", "name", "purpose"],
            },
        },
        {
            "name": "activity_update",
            "description": "Update the current agent's active activity in the requested memory scope, or create one there if none exists",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "task_description": {"type": "string"},
                    "task_note": {"type": "string"},
                    "task_result": {"type": "string"},
                    "status": {"type": "string"},
                    "memory_scope": {"type": "string"},
                    "execution_id": {"type": "string"},
                    "task_ref": {
                        "type": "string",
                        "description": (
                            "The task in another system that this work belongs to, as '<system>:<id>' "
                            "(e.g. 'jira:PROJ-123', 'github:owner/repo#42'). Stored as metadata.task_ref."
                        ),
                    },
                    "handoff": {
                        "type": "object",
                        "description": (
                            "Record when the task finishes or stops: what the next session needs. "
                            "The next session start shows it. A needs_human request replaces your open "
                            "requests in this scope from sessions that started earlier; the response lists "
                            "them as superseded_requests so you can restate any that still apply. With no "
                            "open task, a handoff alone is added to the one your latest task closed with: "
                            "new asks are appended, existing ones are never removed."
                        ),
                        "properties": {
                            "completed": {"type": "string", "description": "One line on what got done."},
                            "next_steps": {"type": "array", "items": {"type": "string"}},
                            "open_questions": {"type": "array", "items": {"type": "string"}},
                            "needs_human": {"type": "string", "description": "What a person must do or decide, if anything."},
                        },
                    },
                },
            },
        },
        {
            "name": "activity_handoff_resolve",
            "description": (
                "Resolve one of your own handoff requests to a person (needs_human) once it has been "
                "dealt with, so it leaves the waiting queue. Only for activities assigned to you."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "activity_id": {"type": "string", "description": "The activity whose handoff request is resolved."},
                    "reason": {
                        "type": "string",
                        "maxLength": 500,
                        "description": "How it was dealt with, in one line. Shown to people and audited.",
                    },
                },
                "required": ["activity_id", "reason"],
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
            "name": "attention_list",
            "description": (
                "List unresolved handoffs that need a person (needs_human), newest first, across the scopes "
                "you can read. For coordinators reporting what is waiting on a person; read-only. Each item "
                "carries task_ref when the work belongs to another system's task."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scopes": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "Limit to these scopes; each must be readable. Default: all readable scopes.",
                    },
                    "without_task_ref": {
                        "type": "boolean",
                        "default": False,
                        "description": "Skip items that point at another system's task, whose own queue already shows them.",
                    },
                    "limit": {"type": "integer", "default": 50},
                },
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
            "name": "workspace_sync",
            "description": "Synchronize pinned context, assigned activities, memory changes, and activity changes since this execution's cursor. Process each stable change id only once even when it appears in more than one group.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_scope": {"type": "string"},
                    "execution_id": {"type": "string"},
                    "after_cursor": {"type": "integer", "minimum": 0},
                    "limit": {"type": "integer", "default": 100, "maximum": 200},
                    "host_session_ref": {"type": "string"},
                },
                "required": ["memory_scope"],
            },
        },
        {
            "name": "session_context",
            "description": (
                "Start a session: one call returns standing rules, unfinished or handed-off work, "
                "recent results, changes since your last session, and an index of key records, "
                "fitted to max_chars, plus the execution_id to use for the session. Call it once at "
                "session start instead of workspace_sync plus several memory searches. Clients with "
                "a SessionStart hook receive this automatically."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_scope": {
                        "type": "string",
                        "description": "Workspace scope. Omit to match the cwd against configured workspace repo roots.",
                    },
                    "cwd": {"type": "string"},
                    "host_session_ref": {"type": "string"},
                    "max_chars": {"type": "integer", "default": 9000, "minimum": 1000, "maximum": 50000},
                },
            },
        },
        {
            "name": "workspace_sync_ack",
            "description": "Acknowledge workspace changes through a delivered cursor for this execution",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "memory_scope": {"type": "string"},
                    "execution_id": {"type": "string"},
                    "cursor": {"type": "integer", "minimum": 0},
                },
                "required": ["memory_scope", "execution_id", "cursor"],
            },
        },
        {
            "name": "effective_authority",
            "description": "Inspect the authenticated request's non-secret effective authority",
            "inputSchema": {"type": "object", "properties": {}},
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
            "name": "connectors_resolve",
            "description": "Resolve one authorized connector binding deterministically; ambiguous candidates fail closed",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector_type_id": {"type": "string"},
                    "logical_alias": {"type": "string"},
                    "scope": {"type": "string"},
                    "action": {"type": "string"},
                },
                "required": ["connector_type_id"],
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
            "description": (
                "List actions for a connector type: name, one-line description, side effect and "
                "required parameters. Pass action=<name> for that action's full input schema "
                "before calling connectors_run."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "connector_type_id": {"type": "string"},
                    "action": {"type": "string", "description": "Return this one action with its full input schema."},
                    "query": {"type": "string", "description": "Substring filter on action names."},
                    "view": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
                    "include_disabled": {"type": "boolean", "default": False},
                    "limit": {"type": "integer", "default": 100, "maximum": 200},
                    "offset": {"type": "integer", "default": 0},
                },
                "required": ["connector_type_id"],
            },
        },
        {
            "name": "connectors_summary",
            "description": "Summarize visible connector types and bindings for the current caller: one line of state per binding (usable, health, failing actions). Pass view='full' or a connector_type_id for credential and health detail.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "scope": {"type": "string"},
                    "connector_type_id": {"type": "string"},
                    "enabled_only": {"type": "boolean", "default": True},
                    "view": {"type": "string", "enum": ["compact", "full"], "default": "compact"},
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
_TOOL_VALIDATORS = {
    tool["name"]: Draft202012Validator(tool["inputSchema"])
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


def _invalid_params_message(tool: str, params) -> str | None:
    if not isinstance(params, dict):
        return "params must be an object"
    validator = _TOOL_VALIDATORS.get(tool)
    if validator is None:
        return None
    # Keep domain-specific enum errors (for example INVALID_CLASS) stable; the
    # manifest validator owns structural/type failures that would otherwise
    # reach handlers as TypeError or ValueError.
    errors = sorted(
        (
            error
            for error in validator.iter_errors(params)
            if error.validator not in {"enum", "required"}
        ),
        key=lambda error: tuple(str(part) for part in error.path),
    )
    if not errors:
        return None
    error = errors[0]
    location = ".".join(str(part) for part in error.path)
    return f"{location}: {error.message}" if location else error.message


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
    tool_name: str, text: str, is_error: bool, ctx: EffectiveAuthority
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
        # Already fitted to its own character budget; offloading it would hide
        # the session context behind a handle the agent may never open.
        or tool_name == "session_context"
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


def _memory_provenance(ctx: EffectiveAuthority, source_kind: str, scope: str) -> str:
    return memory_service.provenance_for_write(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        channel="mcp",
        route="/mcp",
        source_kind=source_kind,
        scope=scope,
        user_id=ctx.user_id,
        agent_id=ctx.agent_id,
        extras=ctx.safe_attribution() if hasattr(ctx, "safe_attribution") else None,
    )


def _compact_connector_tool(tool: dict) -> dict:
    schema = tool.get("input_schema") or {}
    description = " ".join(str(tool.get("description") or "").split())
    return {
        "action": tool["action"],
        "description": description if len(description) <= 160 else description[:159] + "…",
        "side_effect": tool.get("side_effect"),
        "enabled": tool.get("enabled", True),
        "required": list(schema.get("required") or []) if isinstance(schema, dict) else [],
        "capability_policy": tool.get("capability_policy"),
    }


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


def _maybe_attach_digest(
    ctx: EffectiveAuthority,
    memory_scope: str,
    new_activity_id: str,
    execution_id: Optional[str] = None,
) -> Optional[dict]:
    """Best-effort "since you were last active" digest for a fresh activity.

    Returns the digest dict on success, or None on any failure path (so the
    caller omits the `since_last_active` key entirely, never an empty
    placeholder). The caller is responsible for the read-authority gate;
    this helper is only ever called after `enforcer.can_read(memory_scope)`
    has returned True.

    `execution_id` is threaded in from the MCP params, NOT read off `ctx` —
    `EffectiveAuthority` has no `execution_id` field (it's the request
    context for the MCP call, distinct from a workspace-sync execution
    identity). Reading it off `ctx` silently raised AttributeError on
    every real call, which the helper's own try/except then swallowed,
    silently disabling the digest feature in production. See planb.md
    Workstream 1 and the code-review follow-up.

    Order of operations:
    1. If `execution_id` is provided, run `execution_is_caught_up`. A
       genuine caught-up execution means the caller has the fuller picture
       through `workspace_sync` already — the digest would be redundant
       noise. The check excludes the new activity's own change row, which
       was stamped at activity creation and is not something any execution
       could have delivered before this very call.
    2. Otherwise, look up the prior activity for this agent
       in this scope, then call `digest_for_new_arrival`.
    3. Any failure in either step is logged and swallowed — the digest is
       strictly best-effort, the activity itself is already persisted.
    """
    from app.services import system_settings_service, workspace_sync_service

    logger = logging.getLogger(__name__)

    # Feature flag — default on, opt-out per installation.
    try:
        enabled = system_settings_service.read_bool(
            "workspace_awareness_digest_enabled", True
        )
    except Exception:
        enabled = True
    if not enabled:
        return None

    try:
        limit = int(
            system_settings_service.read_int("workspace_awareness_digest_limit", 10)
        )
        limit = max(1, min(limit, 50))
    except Exception:
        limit = 10

    try:
        if execution_id and workspace_sync_service.execution_is_caught_up(
            execution_id,
            memory_scope,
            exclude_activity_id=new_activity_id,
        ):
            return None  # Caller already has the picture via workspace_sync.

        prior = activity_service.get_last_activity_in_scope(
            ctx.agent_id, memory_scope, exclude_id=new_activity_id
        )
        prior_id = prior["id"] if prior else None

        digest = workspace_sync_service.digest_for_new_arrival(
            memory_scope=memory_scope,
            prior_activity_id=prior_id,
            new_activity_id=new_activity_id,
            limit=limit,
        )
        return digest
    except Exception:
        logger.exception(
            "workspace-awareness digest failed for activity %s in %s; omitting",
            new_activity_id,
            memory_scope,
        )
        return None


@router.get("/mcp")
def get_mcp_manifest(ctx: EffectiveAuthority = Depends(get_mcp_request_context)):
    return JSONResponse(content=_manifest_for(ctx))


def _manifest_for(ctx: EffectiveAuthority) -> dict:
    return {
        **MANIFEST,
        "tools": [
            tool
            for tool in MANIFEST["tools"]
            if (required := _TOOL_CAPABILITIES.get(tool["name"])) is None
            or ctx.has_capability(required)
        ],
    }


@router.post("/mcp")
async def handle_mcp_tool(
    request: Request,
    ctx: EffectiveAuthority = Depends(get_mcp_request_context),
):
    try:
        body = await request.json()
    except Exception:
        return _mcp_error("INVALID_REQUEST", "Request body must be valid JSON", 400)

    if not isinstance(body, dict):
        return _mcp_error("INVALID_REQUEST", "Request body must be an object", 400)

    if "jsonrpc" in body:
        if not _is_jsonrpc_request(body) or not isinstance(body["method"], str):
            return _jsonrpc_error(None, -32600, "Invalid JSON-RPC request")
        return await _handle_mcp_jsonrpc(body, request, ctx)

    return await _handle_custom_mcp_tool(body, ctx)


async def _handle_mcp_jsonrpc(body: dict, request: Request, ctx: EffectiveAuthority):
    request_id = body.get("id")
    method = body.get("method")
    if isinstance(request_id, (dict, list, bool)):
        return _jsonrpc_error(None, -32600, "Invalid request id")
    if "params" in body and not isinstance(body["params"], dict):
        return _jsonrpc_error(request_id, -32602, "params must be an object")

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
                    "Send activity_update heartbeats or task_note progress updates every 1-2 minutes while working; when finishing or stopping, set status completed or blocked with task_result and a handoff (completed, next_steps, open_questions, needs_human). "
                    "When a needs_human request you made has been dealt with, resolve it with activity_handoff_resolve. "
                    f"Use credential_get for {CREDENTIAL_PREFIX}* references — never ask the user for raw secrets; use credential_request when one is missing. "
                    "If tools appear unavailable, your host may defer MCP schemas — run the host's tool discovery or schema-load step first."
                ),
            },
        )

    if method == "notifications/initialized":
        return Response(status_code=202)

    if method == "ping":
        return _jsonrpc_response(request_id, {})

    if method == "tools/list":
        return _jsonrpc_response(request_id, {"tools": _manifest_for(ctx)["tools"]})

    if method == "tools/call":
        params = body.get("params") or {}
        tool_name = params.get("name")
        arguments = params.get("arguments", {})
        if not isinstance(tool_name, str) or not tool_name:
            return _jsonrpc_error(request_id, -32602, "Tool name is required")
        if not isinstance(arguments, dict):
            return _jsonrpc_error(request_id, -32602, "arguments must be an object")

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


HANDOFF_RESOLVE_REASON_MAX = 500


# The per-agent limits REST applies to memory writes and searches (see
# docs/security.md). They count against the same buckets, so an agent cannot
# double its budget by switching transport.
_RATE_LIMITED_TOOLS = ("memory_write", "memory_search")


async def _handle_custom_mcp_tool(body: dict, ctx: EffectiveAuthority):
    tool = body.get("tool")
    if tool in _RATE_LIMITED_TOOLS and ctx.agent_id:
        allowed, info = RL.check("agent", ctx.agent_id, tool)
        if not allowed:
            wait = max(0, int(info["reset"] - time.time()))
            return rate_limited_response(
                "RATE_LIMITED", f"{tool} rate limit exceeded; retry in about {wait}s", **info
            )
    if tool == "memory_search" and ctx.agent_id:
        if not CSG.acquire(ctx.agent_id):
            return _mcp_error("CONCURRENT_LIMIT", "Too many concurrent searches", 429)
        try:
            return await _run_custom_mcp_tool(body, ctx)
        finally:
            CSG.release(ctx.agent_id)
    return await _run_custom_mcp_tool(body, ctx)


async def _run_custom_mcp_tool(body: dict, ctx: EffectiveAuthority):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )

    tool = body.get("tool")
    params = body.get("params", {})

    if not isinstance(tool, str) or not tool:
        return _mcp_error("TOOL_REQUIRED", "tool name is required", 400)

    required_capability = _TOOL_CAPABILITIES.get(tool)
    if required_capability and not ctx.has_capability(required_capability):
        return _mcp_error(
            "CAPABILITY_DENIED",
            f"This operation requires the {required_capability} capability",
            403,
        )

    invalid = _invalid_params_message(tool, params)
    if invalid:
        return _mcp_error("INVALID_PARAMS", invalid, 400)

    missing = _missing_required_params(tool, params)
    if missing:
        # Handlers read required parameters directly, so a caller that omits one
        # used to surface as a KeyError and a 500 with a traceback. The manifest
        # already declares what each tool requires, so check against that rather
        # than adding a guard per handler and forgetting one.
        return _mcp_error("INVALID_PARAMS", _missing_params_message(tool, missing), 400)

    if tool == "effective_authority":
        return JSONResponse(
            content={"ok": True, "data": {"authority": ctx.safe_summary()}}
        )
    if tool == "connectors_resolve":
        from app.services import connector_service

        binding = connector_service.resolve_authorized_binding(
            ctx,
            connector_type_id=params["connector_type_id"],
            logical_alias=params.get("logical_alias"),
            scope=params.get("scope"),
            action=params.get("action"),
        )
        return JSONResponse(content={"ok": True, "data": {"binding": binding}})

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
        # Paired with tool_result_offloaded by handle: an offload nobody fetches
        # is a payload the agent never saw, which is what the value report counts.
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="tool_result_fetched",
            resource_type="tool_result_spill",
            resource_id=handle,
            result="success",
            details={
                "tool": result["tool"],
                "offset": result["offset"],
                "returned_chars": result["returned_chars"],
                "total_chars": result["total_chars"],
                "has_more": result["has_more"],
            },
        )
        return JSONResponse(content={"ok": True, "data": result})

    if tool == "memory_search":
        try:
            query_text = validate_search_query(params.get("query", ""))
            max_response_chars = validate_response_budget(params.get("max_response_chars"))
        except MemoryOperationError as exc:
            return _mcp_error(exc.code, exc.message, exc.status_code)
        if max_response_chars is None and settings.TOOL_RESULT_SPILL_THRESHOLD > 0:
            # Without a budget, an oversized result is offloaded and the agent
            # sees a preview instead of the records it searched for — a quarter
            # of searches on the first deployment. Trimming to whole records
            # under the offload threshold delivers the best matches inline, and
            # record_recall below then counts only what the agent received.
            max_response_chars = settings.TOOL_RESULT_SPILL_THRESHOLD
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
            if not ctx.can("memory", "read", scope=search_scope):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            allowed = [search_scope]
        else:
            candidates = ctx.default_recall_scopes or ctx.read_scopes
            allowed = ctx.filter_scopes("memory", "read", candidates)
        if not allowed:
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
                    max_response_chars,
                )
            except MemoryOperationError as exc:
                return _mcp_error(exc.code, exc.message, exc.status_code)
            return JSONResponse(content={"ok": True, "data": data})
        try:
            records, mode = await asyncio.to_thread(
                memory_service.search_memory,
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
        embedding_status = await asyncio.to_thread(
            embedding_service.safe_backend_status
        )
        try:
            data = budget_search_response(
                {
                    "records": records,
                    "retrieval_mode": mode,
                    "embedding_backend_status": embedding_service.backend_label(
                        embedding_status
                    ),
                    "total": len(records),
                },
                max_response_chars,
            )
        except MemoryOperationError as exc:
            return _mcp_error(exc.code, exc.message, exc.status_code)
        records = data["records"]
        await asyncio.to_thread(
            memory_service.record_recall, [r["id"] for r in records]
        )
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
                "embedding_backend_status": embedding_service.backend_label(
                    embedding_status
                ),
            },
        )
        if mode == "fts_only" and embedding_service.retrieval_is_degraded(
            embedding_status
        ):
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
        return JSONResponse(content={"ok": True, "data": data})

    elif tool == "memory_get":
        # Compact view (lean metadata + content preview) so a whole scope can be
        # surveyed inline without blowing the tool-output budget. In compact view
        # rows are tiny, so allow a higher cap.
        view = params.get("view")
        if view not in (None, "full", "compact"):
            return _mcp_error("INVALID_PARAMS", "view must be 'full' or 'compact'", 400)
        if params.get("ids") is not None:
            try:
                data = await asyncio.to_thread(
                    fetch_memory_by_ids, params["ids"], ctx, view=view, channel="mcp"
                )
            except MemoryOperationError as exc:
                return _mcp_error(exc.code, exc.message, exc.status_code)
            return JSONResponse(content={"ok": True, "data": data})
        if params.get("scope"):
            if not ctx.can("memory", "read", scope=params["scope"]):
                return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
            records = memory_service.get_memory_by_scope(
                scope=params["scope"],
                limit=min(params.get("limit", 50), 200),
                offset=params.get("offset", 0),
                record_status=params.get("record_status"),
            )
        else:
            candidates = ctx.default_recall_scopes or ctx.read_scopes
            allowed = ctx.filter_scopes("memory", "read", candidates)
            records = memory_service.get_memory_by_scopes(
                scopes=allowed,
                limit=min(params.get("limit", 50), 200),
                offset=params.get("offset", 0),
                record_status=params.get("record_status"),
            )
        if view is None:
            # Auto: compact for large pages, full for small (back-compat for
            # callers reading a handful of records).
            view = "compact" if len(records) > _MEMORY_COMPACT_THRESHOLD else "full"
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
        try:
            payload = await run_memory_write(params, ctx, channel="mcp", route="/mcp")
        except MemoryOperationError as exc:
            return _mcp_error(exc.code, exc.message, exc.status_code)
        return JSONResponse(content={"ok": True, "data": payload}, status_code=201)

    elif tool == "memory_pinned":
        candidates = ctx.default_recall_scopes or ctx.read_scopes
        scopes = ctx.filter_scopes("memory", "read", candidates)
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
        if not ctx.can("memory", "write", scope=record["scope"]):
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
            details={
                "pin": desired,
                "scope": record["scope"],
                "proposal_id": proposal_id,
            },
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
        try:
            payload = await run_memory_confirm(
                params["record_id"],
                evidence=params.get("evidence") or "",
                authority=ctx,
                channel="mcp",
                route="/mcp memory_confirm",
            )
        except MemoryOperationError as exc:
            return _mcp_error(exc.code, exc.message, exc.status_code)
        return JSONResponse(content={"ok": True, "data": payload})

    elif tool == "memory_reanchor":
        record = memory_service.get_memory_record(params["record_id"])
        if not record:
            return _mcp_error("NOT_FOUND", "Record not found", 404)
        if not ctx.can("memory", "write", scope=record["scope"]):
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
            return _mcp_error(
                "NOT_ACTIVE", "Only an active record can be re-anchored", 400
            )
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
            content={
                "ok": True,
                "data": {"record": memory_service.lean_record(updated)},
            }
        )

    elif tool == "memory_verify":
        from app.services import verification_service

        verify_scope = params.get("scope")
        if verify_scope and not ctx.can("memory", "write", scope=verify_scope):
            # Verification writes confirmation onto records, so it needs the
            # same authority as any other statement about a scope.
            return _mcp_error("SCOPE_DENIED", "Access denied to this scope", 403)
        if not verify_scope:
            candidates = list(ctx.write_scopes)
            writable = ctx.filter_scopes("memory", "write", candidates)
            if not writable:
                return _mcp_error("SCOPE_DENIED", "No writable scope to verify", 403)

        result = await asyncio.to_thread(
            verification_service.verify_scope,
            scope=verify_scope,
            limit=min(int(params.get("limit", 50) or 50), 200),
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
        if not ctx.can("memory", "read", scope=record["scope"]):
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
        if not ctx.can("memory", "write", scope=record["scope"]):
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
        if not ctx.can("memory", "write", scope=record["scope"]):
            return _mcp_error("SCOPE_DENIED", "Access denied to source scope", 403)
        if not ctx.can("memory", "write", scope=new_scope):
            return _mcp_error("SCOPE_DENIED", "Access denied to destination scope", 403)
        source_kind = params.get("source_kind", "agent_inference")
        if source_kind not in SOURCE_KINDS:
            return _mcp_error(
                "INVALID_SOURCE_KIND", f"source_kind must be one of {SOURCE_KINDS}", 400
            )
        denied = validate_source_kind_authority(source_kind, ctx)
        if denied is not None:
            return _mcp_error(denied[0], denied[1], 403)
        new_record, err = memory_service.move_memory(
            record_id=params["record_id"],
            new_scope=new_scope,
            provenance_json=_memory_provenance(ctx, source_kind, new_scope),
        )
        if err == "NOT_FOUND":
            return _mcp_error("NOT_FOUND", "Memory record not found", 404)
        if err == "NOT_ACTIVE":
            return _mcp_error(
                "INVALID_STATE", "Only an active record can be moved", 400
            )
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

    elif tool == "credential_request":
        from app.services import credential_request_service

        if any(not isinstance(params.get(f) or "", str) for f in ("scope", "name", "purpose")):
            return _mcp_error("INVALID_PARAMS", "scope, name and purpose must be strings", 400)
        scope = (params.get("scope") or "").strip()
        name = (params.get("name") or "").strip()
        purpose = (params.get("purpose") or "").strip()
        if not scope or not name or not purpose:
            return _mcp_error("INVALID_PARAMS", "scope, name and purpose are required", 400)
        if len(name) > 100 or len(purpose) > 1000:
            return _mcp_error("INVALID_PARAMS", "name is limited to 100 and purpose to 1000 characters", 400)
        from app.services.credential_service import normalize_scope

        try:
            scope = normalize_scope(scope)  # check write on the scope it will be stored in
        except ValueError:
            return _mcp_error("INVALID_SCOPE", "Malformed scope", 400)
        if not enforcer.can_write(scope):
            return _mcp_error("SCOPE_DENIED", "You cannot store credentials in this scope", 403)
        try:
            result = credential_request_service.request_credential(
                agent_id=ctx.agent_id, user_id=ctx.user_id, scope=scope, name=name, purpose=purpose
            )
        except ValueError as exc:
            return _mcp_error("TOO_MANY_REQUESTS", str(exc), 429)
        if result.get("created"):
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="credential_requested",
                resource_type="credential_request",
                resource_id=result["request_id"],
                result="success",
                details={"scope": result["scope"], "name": name},
            )
        return JSONResponse(content={"ok": True, "data": result})

    elif tool == "activity_update":
        # An agent identity can work in more than one workspace at once. The
        # requested scope identifies that activity stream; never select a
        # different active activity and then move it into this scope.
        memory_scope = params.get("memory_scope") or f"agent:{ctx.agent_id}"
        if not enforcer.can_write(memory_scope):
            return _mcp_error("SCOPE_DENIED", "Access denied to memory_scope", 403)
        if params.get("execution_id"):
            from app.services import workspace_sync_service

            try:
                workspace_sync_service.validate_execution(
                    execution_id=params["execution_id"],
                    agent_id=ctx.agent_id,
                    user_id=ctx.user_id or "",
                    memory_scope=memory_scope,
                )
            except PermissionError:
                return _mcp_error(
                    "EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403
                )
            except ValueError as exc:
                return _mcp_error(str(exc), "Invalid execution", 400)
        handoff = None
        if params.get("handoff") is not None:
            from app.services.session_context_service import normalize_handoff

            try:
                handoff = normalize_handoff(params["handoff"])
            except ValueError as exc:
                return _mcp_error("INVALID_HANDOFF", str(exc), 400)
        task_ref = params.get("task_ref")
        if task_ref is not None:
            try:
                activity_service.validate_task_ref(task_ref)
            except ValueError as exc:
                return _mcp_error("INVALID_TASK_REF", str(exc), 400)
        existing = activity_service.get_active_activity_for_agent(
            ctx.agent_id, ctx.user_id, memory_scope=memory_scope
        )
        if existing:
            metadata_json = (
                activity_service.with_task_ref(existing.get("metadata_json"), task_ref)
                if task_ref is not None
                else None
            )
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
                    status=params["status"],
                    metadata_json=metadata_json,
                    source_execution_id=params.get("execution_id"),
                )
            elif (
                params.get("task_description")
                or params.get("task_note")
                or params.get("task_result")
                or metadata_json
            ):
                activity_service.update_activity(
                    existing["id"],
                    task_description=params.get("task_description"),
                    task_note=params.get("task_note"),
                    task_result=params.get("task_result"),
                    metadata_json=metadata_json,
                    source_execution_id=params.get("execution_id"),
                )
            else:
                activity_service.heartbeat_activity(existing["id"])
            if handoff:  # after the transition check, so a refused close writes nothing
                activity_service.set_handoff(existing["id"], handoff)
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
                    else "update",
                    previous_status=existing["status"],
                    new_status=params.get("status") or existing["status"],
                    memory_scope=existing.get("memory_scope"),
                ),
            )
            _new_status = params.get("status")
            if (
                not _new_status
                and not params.get("task_description")
                and not params.get("task_note")
                and not params.get("task_result")
            ):
                _event = "activity_heartbeat"
            elif _new_status == "cancelled":
                _event = "activity_cancelled"
            else:
                _event = "activity_updated"
            activity_service.notify(
                _event, _updated, previous_status=existing["status"]
            )
            data = {
                "activity": _updated,
                **activity_service.handoff_followups(
                    _updated,
                    handoff,
                    closing=_new_status in ("completed", "blocked"),
                    actor_type="agent",
                    actor_id=ctx.agent_id,
                ),
            }
            return JSONResponse(content={"ok": True, "data": data})
        elif handoff and not any(
            params.get(key) for key in ("task_description", "task_note", "task_result", "status", "task_ref")
        ) and (
            closed := activity_service.get_latest_closed_activity_for_agent(
                ctx.agent_id, memory_scope, user_id=ctx.user_id or ""
            )
        ):
            # A handoff on its own, with no open task: amend the handoff the
            # agent's latest task closed with, for example to restate a request
            # that handoff superseded. Amending adds; it never drops an ask.
            try:
                activity_service.set_handoff(closed["id"], handoff, amend=True)
            except ValueError as exc:
                return _mcp_error("INVALID_HANDOFF", str(exc), 400)
            _updated = activity_service.get_activity(closed["id"]) or closed
            audit_service.write_event(
                actor_type="agent",
                actor_id=ctx.agent_id,
                action="activity_update",
                resource_type="activity",
                resource_id=closed["id"],
                result="success",
                details=activity_service.audit_details(
                    _updated, action="handoff_amended", memory_scope=closed.get("memory_scope")
                ),
            )
            activity_service.notify("activity_updated", _updated, previous_status=closed["status"])
            data = {
                "activity": _updated,
                "handoff_amended": True,
                **activity_service.handoff_followups(
                    _updated, handoff, closing=False, actor_type="agent", actor_id=ctx.agent_id
                ),
            }
            return JSONResponse(content={"ok": True, "data": data})
        else:
            if not params.get("task_description"):
                return _mcp_error(
                    "TASK_REQUIRED", "task_description required to create activity", 400
                )
            act = activity_service.create_activity(
                agent_id=ctx.agent_id,
                user_id=ctx.user_id or "",
                task_description=params["task_description"],
                memory_scope=memory_scope,
                metadata_json=activity_service.with_task_ref(None, task_ref) if task_ref else None,
                source_execution_id=params.get("execution_id"),
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

            # Best-effort "since you were last active" digest for the new
            # activity. Only ever attaches to a freshly created activity in a
            # `workspace:*` scope, never to heartbeats/updates, and never to a
            # caller that lacks read authority on the scope. Activity creation
            # is already complete at this point — a digest failure cannot
            # prevent the activity from existing. See planb.md Workstream 1.
            payload: dict = {"activity": act}
            if (
                memory_scope
                and memory_scope.startswith("workspace:")
                and enforcer.can_read(memory_scope)
            ):
                digest = _maybe_attach_digest(
                    ctx,
                    memory_scope,
                    act["id"],
                    execution_id=params.get("execution_id"),
                )
                # `_maybe_attach_digest` swallows its own errors and returns
                # None. The outer try/except below is a belt-and-braces for
                # anything that escapes it — `since_last_active` is purely
                # additive, and a failure here must never fail the create.
                if digest is not None:
                    payload["since_last_active"] = digest
            return JSONResponse(content={"ok": True, "data": payload}, status_code=201)

    elif tool == "activity_handoff_resolve":
        reason = " ".join(str(params.get("reason") or "").split())
        if not reason:
            return _mcp_error("INVALID_PARAMS", "reason is required: say how the request was dealt with", 400)
        if len(reason) > HANDOFF_RESOLVE_REASON_MAX:
            return _mcp_error(
                "INVALID_PARAMS", f"reason is limited to {HANDOFF_RESOLVE_REASON_MAX} characters", 400
            )
        activity = activity_service.get_activity(params["activity_id"])
        if not activity:
            return _mcp_error("NOT_FOUND", "Activity not found", 404)
        # The agent rule of the REST _can_activity check: only the agent the
        # activity is assigned to, and only in a scope it can write.
        scope = activity.get("memory_scope") or f"agent:{activity.get('agent_id')}"
        if activity.get("assigned_agent_id") != ctx.agent_id or not enforcer.can_write(scope):
            return _mcp_error(
                "FORBIDDEN", "You can resolve only your own handoff requests, in scopes you can write", 403
            )
        resolution = activity_service.resolve_handoff(
            activity["id"], kind="agent", resolved_by=ctx.agent_id, reason=reason
        )
        if not resolution:
            return _mcp_error("NOT_PENDING", "No unresolved handoff request on this activity", 409)
        _updated = activity_service.get_activity(activity["id"]) or activity
        audit_service.write_event(
            actor_type="agent",
            actor_id=ctx.agent_id,
            action="handoff_resolved",
            resource_type="activity",
            resource_id=activity["id"],
            result="success",
            details=activity_service.audit_details(
                _updated, kind="agent", actor_type="agent", reason=reason
            ),
        )
        activity_service.notify("activity_handoff_resolved", _updated, handoff_resolution="agent")
        return JSONResponse(
            content={"ok": True, "data": {"activity": _updated, "resolution": resolution}}
        )

    elif tool == "activity_get":
        activity = activity_service.get_activity(params["activity_id"])
        if not activity:
            return _mcp_error("NOT_FOUND", "Activity not found", 404)
        # Same visibility as activity_list: own work, or any activity in a
        # readable scope.
        if (
            activity.get("agent_id") != ctx.agent_id
            and activity.get("assigned_agent_id") != ctx.agent_id
            and not enforcer.can_read(activity_service.activity_scope(activity))
        ):
            return _mcp_error("FORBIDDEN", "Access denied", 403)
        # What the session concluded, scope-filtered for this caller: the task
        # description says what was attempted, these say what came of it.
        produced = memory_service.records_for_activity(
            params["activity_id"],
            authorized_scopes=(
                None
                if ctx.is_admin
                else enforcer.filter_readable_scopes(ctx.read_scopes)
            ),
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
        enforcer = ScopeEnforcer(
            ctx.read_scopes,
            ctx.write_scopes,
            ctx.agent_id,
            is_admin=ctx.is_admin,
            active_workspace_ids=ctx.active_workspace_ids,
        )
        authorized_scopes = (
            None
            if ctx.is_admin
            else enforcer.filter_readable_scopes(ctx.read_scopes)
        )
        activities = activity_service.list_activities(
            status=status,
            agent_id=agent_filter,
            assigned_agent_id=assigned_filter,
            authorized_scopes=authorized_scopes,
            limit=limit,
            offset=offset,
        )
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
        authorized_scopes = (
            None
            if ctx.is_admin
            else enforcer.filter_readable_scopes(ctx.read_scopes)
        )
        activities = activity_service.search_activities(
            query,
            agent_id=params.get("agent_id"),
            status=params.get("status"),
            memory_scope=params.get("memory_scope"),
            since=params.get("since"),
            authorized_scopes=authorized_scopes,
            limit=limit,
            offset=offset,
        )
        return JSONResponse(
            content={
                "ok": True,
                "data": {"activities": activities, "count": len(activities)},
            }
        )

    elif tool == "attention_list":
        requested = params.get("scopes")
        if requested is not None and (
            not isinstance(requested, list) or not all(isinstance(s, str) for s in requested)
        ):
            return _mcp_error("INVALID_PARAMS", "scopes must be a list of strings", 400)
        if requested:
            denied = next((s for s in requested if not enforcer.can_read(s)), None)
            if denied:
                return _mcp_error("SCOPE_DENIED", f"Access denied to scope: {denied}", 403)
            authorized_scopes = list(dict.fromkeys(requested))
        else:
            authorized_scopes = (
                None if ctx.is_admin else enforcer.filter_readable_scopes(ctx.read_scopes)
            )
        try:
            limit = min(max(int(params.get("limit", 50) or 50), 0), 200)
        except (TypeError, ValueError):
            return _mcp_error("INVALID_PARAMS", "limit must be an integer", 400)
        items = activity_service.list_needs_human(
            authorized_scopes=authorized_scopes,
            without_task_ref=bool(params.get("without_task_ref")),
            limit=limit,
        )
        return JSONResponse(
            content={"ok": True, "data": {"items": items, "count": len(items)}}
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
                    "message": None
                    if activity
                    else "No assigned work found for this agent in authorized scopes",
                },
            }
        )

    elif tool == "session_context":
        from app.routes.session_context import readable_workspace_scopes
        from app.services import session_context_service

        if not ctx.agent_id:
            return _mcp_error("AGENT_REQUIRED", "Session context requires an agent", 400)
        requested = params.get("memory_scope")
        for field in ("memory_scope", "cwd", "host_session_ref"):
            if params.get(field) is not None and not isinstance(params[field], str):
                return _mcp_error("INVALID_PARAMS", f"{field} must be a string", 400)
        if requested and (
            not requested.startswith("workspace:") or not enforcer.can_read(requested)
        ):
            return _mcp_error("SCOPE_DENIED", "Access denied to this workspace", 403)
        readable = readable_workspace_scopes(ctx)
        if requested and requested not in readable:
            readable.append(requested)
        try:
            budget = int(params.get("max_chars", session_context_service.DEFAULT_BUDGET_CHARS))
        except (TypeError, ValueError):
            return _mcp_error("INVALID_PARAMS", "max_chars must be an integer", 400)
        result = await asyncio.to_thread(
            session_context_service.build_session_context,
            agent_id=ctx.agent_id,
            user_id=ctx.user_id or "",
            readable_scopes=readable,
            memory_scope=requested,
            cwd=params.get("cwd"),
            host_session_ref=params.get("host_session_ref"),
            source="mcp",
            budget=budget,
            channel="mcp",
        )
        # The id lists are for the audit trail; the agent reads them in the text.
        result.pop("memory_ids", None)
        result.pop("activity_ids", None)
        return JSONResponse(content={"ok": True, "data": result})

    elif tool in ("workspace_sync", "workspace_sync_ack"):
        from app.services import workspace_sync_service

        memory_scope = params["memory_scope"]
        if not enforcer.can_read(memory_scope):
            return _mcp_error("SCOPE_DENIED", "Access denied to this workspace", 403)
        try:
            if tool == "workspace_sync":
                result = workspace_sync_service.sync_workspace(
                    agent_id=ctx.agent_id,
                    user_id=ctx.user_id or "",
                    memory_scope=memory_scope,
                    execution_id=params.get("execution_id"),
                    after_cursor=params.get("after_cursor"),
                    limit=params.get("limit", 100),
                    host_session_ref=params.get("host_session_ref"),
                )
            else:
                result = workspace_sync_service.acknowledge(
                    agent_id=ctx.agent_id,
                    user_id=ctx.user_id or "",
                    execution_id=params["execution_id"],
                    memory_scope=memory_scope,
                    cursor=params["cursor"],
                )
        except PermissionError:
            return _mcp_error(
                "EXECUTION_OWNERSHIP", "Execution belongs to another agent", 403
            )
        except ValueError as exc:
            return _mcp_error(str(exc), "Invalid execution or cursor", 400)
        return JSONResponse(content={"ok": True, "data": result})

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
        result = await asyncio.to_thread(
            connector_service.test_binding, params["binding_id"]
        )
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
        wanted = params.get("action")
        view = "full" if wanted else params.get("view", "compact")
        if view not in ("compact", "full"):
            return _mcp_error("INVALID_PARAMS", "view must be 'compact' or 'full'", 400)
        tool_result = await asyncio.to_thread(
            connector_service.generate_connector_type_tools,
            ct,
            disabled_actions=ct.get("disabled_actions") or [],
            include_disabled=bool(params.get("include_disabled", False)),
            query=wanted or params.get("query"),
            limit=200 if wanted else min(int(params.get("limit", 100) or 100), 200),
            offset=0 if wanted else max(int(params.get("offset", 0) or 0), 0),
        )
        tools = tool_result["tools"]
        if wanted:
            tools = [t for t in tools if t["action"] == wanted]
            if not tools:
                return _mcp_error("NOT_FOUND", f"No action named {wanted!r}", 404)
        elif view == "compact":
            # Full input schemas made this one of the most-offloaded tools: an
            # agent needs the menu first and one action's schema second.
            tools = [_compact_connector_tool(t) for t in tools]

        return JSONResponse(
            content={
                "ok": True,
                "data": {
                    "connector_type_id": ct["id"],
                    "display_name": ct["display_name"],
                    "auth_type": ct["auth_type"],
                    "total": tool_result.get("total", len(tools)),
                    "view": view,
                    "actions": [t["action"] for t in tools],
                    "tools": tools,
                },
            }
        )

    elif tool == "connectors_summary":
        from app.services import connector_service

        summary = await asyncio.to_thread(
            connector_service.build_capability_summary,
            enforcer,
            connector_type_id=params.get("connector_type_id"),
            scope=params.get("scope"),
            enabled_only=bool(params.get("enabled_only", True)),
        )
        view = params.get("view") or ("full" if params.get("connector_type_id") else "compact")
        if view not in ("compact", "full"):
            return _mcp_error("INVALID_PARAMS", "view must be 'compact' or 'full'", 400)
        if view == "compact":
            # The overview never fit inline in its full form. Per binding, the
            # agent needs to know whether it can use it and whether it is healthy.
            summary = {
                **summary,
                "view": "compact",
                "connectors": [
                    {
                        "id": c["id"],
                        "display_name": c["display_name"],
                        "action_count": c["action_count"],
                        "bindings": [
                            {
                                "id": b["id"],
                                "name": b["name"],
                                "scope": b["scope"],
                                "usable": b["usable_by_caller"],
                                "health": b["health"]["test_status"],
                                "failing_actions": len(b["health"]["failing_actions"]),
                            }
                            for b in c["bindings"]
                        ],
                    }
                    for c in summary["connectors"]
                ],
            }
        return JSONResponse(content={"ok": True, "data": summary})

    elif tool == "connectors_run":
        action = params["action"]
        outcome = await asyncio.to_thread(
            run_connector_action,
            params["binding_id"],
            action,
            params.get("params") or {},
            ctx,
        )
        if not outcome.ok:
            return _mcp_error(outcome.error_code, outcome.error_message, 200)
        binding = outcome.binding
        result = outcome.result
        if result.get("success") and "body" in result:
            new_body, exported_count = artifact_export_service.export_connector_body(
                result["body"]
            )
            if exported_count > 0:
                result["body"] = new_body
                result["artifacts_exported"] = exported_count
                audit_service.write_event(
                    actor_type=ctx.actor_type,
                    actor_id=ctx.actor_id,
                    action="connector_artifact_exported",
                    resource_type="connector_binding",
                    resource_id=params["binding_id"],
                    result="success",
                    details={
                        "connector_type_id": binding["connector_type_id"],
                        "action": params["action"],
                        "count": exported_count,
                    },
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

        return JSONResponse(content={"ok": True, "data": result})

    else:
        return _mcp_error("UNKNOWN_TOOL", f"Unknown tool: {tool}", 400)
