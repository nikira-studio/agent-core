from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.response_helpers import success_response
from app.branding import CREDENTIAL_PREFIX
from app.config import settings
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS


router = APIRouter()


class SpecResponse(BaseModel):
    version: str
    base_url: str
    mcp_endpoint: str
    rest_api_prefix: str
    auth_methods: list[str]
    session_duration_hours: int
    inactivity_timeout_minutes: int
    scope_model: dict
    rest_endpoints: list[dict]
    mcp_tools: list[dict]
    broker_behavior: dict
    rate_limits: dict
    backup_restore: dict
    feature_flags: dict


@router.get("/spec")
def spec(
    ctx: RequestContext = Depends(get_request_context),
):
    scope_model = {
        "description": "Agents access memory and credentials through scopes: agent:<id>, user:<id>, workspace:<id>, shared",
        "read_scopes": "Scopes the principal can read from",
        "write_scopes": "Scopes the principal can write to",
        "shared": "Broad shared scope; shared write requires explicit grant",
        "scope_ceiling": "Maximum scopes an agent can access; agents belong to one owner/default user and use workspace:<id> workspaces for collaboration",
    }

    rest_endpoints = [
        {
            "prefix": "/api/auth",
            "methods": ["POST"],
            "description": "Login, register, OTP verify, logout",
        },
        {
            "prefix": "/api/memory",
            "methods": ["POST", "GET"],
            "description": "Memory write, search, get, retract, detail",
        },
        {
            "prefix": "/api/credentials",
            "methods": ["POST", "GET"],
            "description": "Credential entry create, list, reveal",
        },
        {
            "prefix": "/api/agents",
            "methods": ["POST", "GET"],
            "description": "Agent create, list, rotate key, deactivate",
        },
        {
            "prefix": "/api/workspaces",
            "methods": ["POST", "GET"],
            "description": "Workspace CRUD",
        },
        {
            "prefix": "/api/activity",
            "methods": ["POST", "GET"],
            "description": "Activity create, list, update, heartbeat",
        },
        {
            "prefix": "/api/connector-types",
            "methods": ["GET", "POST", "PUT", "DELETE"],
            "description": "Connector type discovery, OpenAPI import, MCP import/refresh, and action discovery",
        },
        {
            "prefix": "/api/connector-bindings",
            "methods": ["GET", "POST", "PUT", "DELETE"],
            "description": "Connector bindings, testing, execution history, and binding run support",
        },
        {
            "prefix": "/api/connector-types/import-mcp",
            "methods": ["POST"],
            "description": "Register a native MCP server as a connector type",
        },
        {
            "prefix": "/api/connector-types/{connector_type_id}/refresh",
            "methods": ["POST"],
            "description": "Refresh discovery metadata for a native MCP connector type",
        },
        {
            "prefix": "/api/connector-bindings/{binding_id}/run",
            "methods": ["POST"],
            "description": "Run a connector action through a stored binding",
        },
        {
            "prefix": "/api/briefings",
            "methods": ["POST", "GET"],
            "description": "Briefing generate and retrieve",
        },
        {
            "prefix": "/api/backup",
            "methods": ["GET", "POST"],
            "description": "Backup export/restore, partial exports",
        },
        {
            "prefix": "/mcp",
            "methods": ["GET", "POST"],
            "description": "MCP manifest and tool calls",
        },
    ]

    mcp_tools = [
        {
            "name": "memory_search",
            "description": "Search memory records by text query; no scope = your default recall scopes, scope = one specific readable scope on demand",
            "inputSchema": {
                "query": "string",
                "scope": "string?",
                "topic": "string?",
                "memory_class": list(MEMORY_CLASSES),
                "min_confidence": "number?",
                "subject_anchor": "string?",
                "activity_id": "string?",
                "as_of": "string?",
                "limit": "integer?",
                "include_retracted": "boolean?",
                "include_superseded": "boolean?",
            },
        },
        {
            "name": "memory_get",
            "description": "Get memory records by scope or list active records; view='compact' surveys a scope (metadata + preview), defaults compact for large pages",
            "inputSchema": {
                "scope": "string?",
                "record_status": "string?",
                "view": "string?",
                "limit": "integer?",
                "offset": "integer?",
            },
        },
        {
            "name": "memory_write",
            "description": "Write a new memory record",
            "inputSchema": {
                "content": "string",
                "memory_class": list(MEMORY_CLASSES),
                "scope": "string",
                "topic": "string?",
                "confidence": "number?",
                "importance": "number?",
                "source_kind": list(SOURCE_KINDS),
                "supersedes_id": "string?",
                "slot_key": "string?",
                "valid_from": "string?",
                "valid_to": "string?",
                "last_confirmed_at": "string?",
            },
        },
        {
            "name": "memory_retract",
            "description": "Retract a memory record by ID",
            "inputSchema": {"record_id": "string"},
        },
        {
            "name": "memory_move",
            "description": "Atomically relocate an active record to a new scope (write access to both scopes required)",
            "inputSchema": {"record_id": "string", "new_scope": "string", "source_kind": "string?"},
        },
        {
            "name": "credential_get",
            "description": "Get a credential reference name by entry ID",
            "inputSchema": {"entry_id": "string"},
        },
        {
            "name": "credential_list",
            "description": "List credential references in authorized scopes",
            "inputSchema": {"scope": "string?", "limit": "integer?"},
        },
        {
            "name": "activity_update",
            "description": "Update the current agent's active activity in the requested memory scope, or create one there if none exists",
            "inputSchema": {
                "task_description": "string?",
                "task_note": "string?",
                "task_result": "string?",
                "status": "string?",
                "memory_scope": "string?",
            },
        },
        {
            "name": "activity_get",
            "description": "Get a specific activity by ID",
            "inputSchema": {"activity_id": "string"},
        },
        {
            "name": "activity_list",
            "description": "List activities visible to the current agent or user",
            "inputSchema": {
                "status": "string?",
                "agent_id": "string?",
                "assigned_agent_id": "string?",
                "limit": "integer?",
                "offset": "integer?",
            },
        },
        {
            "name": "memory_pinned",
            "description": "Standing context for your scopes — loaded at session start rather than searched for",
            "inputSchema": {},
        },
        {
            "name": "memory_pin",
            "description": "Pin a record as standing context, or unpin it (capped per scope)",
            "inputSchema": {"record_id": "string", "pinned": "boolean?"},
        },
        {
            "name": "memory_confirm",
            "description": "Mark a record as still current as of now, clearing its staleness",
            "inputSchema": {"record_id": "string"},
        },
        {
            "name": "memory_reanchor",
            "description": "Repoint a record at what actually describes it when its anchor is wrong or the file moved",
            "inputSchema": {"record_id": "string", "subject_anchor": "string"},
        },
        {
            "name": "memory_verify",
            "description": "Check anchored facts against the repo path or connector binding they describe; queues review for anchors that have vanished",
            "inputSchema": {"scope": "string?", "limit": "integer?"},
        },
        {
            "name": "memory_feedback",
            "description": "Say whether a recalled record actually helped; feeds observed usefulness into ranking",
            "inputSchema": {"record_id": "string", "helpful": "boolean"},
        },
        {
            "name": "activity_search",
            "description": "Full-text search the activity trail (what agents worked on, per task, with results); use memory_search for durable facts and decisions",
            "inputSchema": {
                "query": "string",
                "agent_id": "string?",
                "memory_scope": "string?",
                "status": "string?",
                "since": "string?",
                "limit": "integer?",
                "offset": "integer?",
            },
        },
        {
            "name": "activity_pickup",
            "description": "Claim the next active work item assigned to this agent in authorized scopes",
            "inputSchema": {},
        },
        {
            "name": "connectors_list",
            "description": "List installed connector types as lean summaries (no full specs); use connectors_actions_list for a type's actions",
            "inputSchema": {"limit": "integer?", "offset": "integer?"},
        },
        {
            "name": "connectors_resolve",
            "description": "Resolve one authorized connector binding deterministically",
            "inputSchema": {"connector_type_id": "string", "logical_alias": "string?", "scope": "string?", "action": "string?"},
        },
        {
            "name": "delegation_request",
            "description": "Request narrowly scoped authority for a recipient agent",
            "inputSchema": {"recipient_agent_id": "string", "purpose": "string", "ttl_seconds": "integer", "scope_permissions": "array?", "resource_permissions": "array?", "binding_actions": "array?"},
        },
        {
            "name": "effective_authority",
            "description": "Inspect non-secret effective authority",
            "inputSchema": {},
        },
        {
            "name": "delegations_list",
            "description": "List visible non-secret delegated grants",
            "inputSchema": {},
        },
        {
            "name": "delegation_requests_list",
            "description": "List visible delegation requests",
            "inputSchema": {},
        },
        {
            "name": "delegation_request_approve",
            "description": "Approve and optionally narrow a pending delegation request",
            "inputSchema": {"request_id": "string", "scope_permissions": "array?", "resource_permissions": "array?", "binding_actions": "array?"},
        },
        {
            "name": "delegation_request_deny",
            "description": "Deny a pending delegation request",
            "inputSchema": {"request_id": "string", "reason": "string?"},
        },
        {
            "name": "delegation_revoke",
            "description": "Immediately revoke an issued or received grant",
            "inputSchema": {"grant_id": "string", "reason": "string?"},
        },
        {
            "name": "connectors_summary",
            "description": "Summarize visible connector types, bindings, credentials, actions, and health state for the current caller",
            "inputSchema": {"scope": "string?", "connector_type_id": "string?", "enabled_only": "boolean?"},
        },
        {
            "name": "connectors_actions_list",
            "description": "List actions available for a connector type",
            "inputSchema": {"connector_type_id": "string"},
        },
        {
            "name": "connectors_bindings_list",
            "description": "List connector bindings in authorized scopes",
            "inputSchema": {"scope": "string?", "connector_type_id": "string?", "enabled_only": "boolean?"},
        },
        {
            "name": "connectors_bindings_test",
            "description": "Test a connector binding",
            "inputSchema": {"binding_id": "string"},
        },
        {
            "name": "connectors_run",
            "description": "Execute a connector action through a stored binding",
            "inputSchema": {"binding_id": "string", "action": "string", "params": "object?"},
        },
        {
            "name": "get_briefing",
            "description": "Get a handoff briefing by ID",
            "inputSchema": {"briefing_id": "string"},
        },
        {
            "name": "briefing_list",
            "description": "List generated briefings visible to the current agent or user",
            "inputSchema": {"agent_id": "string?", "limit": "integer?", "offset": "integer?"},
        },
        {
            "name": "result_fetch",
            "description": "Retrieve a slice of a previously offloaded large tool result by handle",
            "inputSchema": {"handle": "string", "offset": "integer?", "limit": "integer?"},
        },
    ]

    broker_behavior = {
        "description": "Credential values are resolved internally by the credential broker and never exposed in API responses, prompts, or logs",
        "resolve_endpoint": "/internal/credentials/resolve",
        "resolve_auth": "broker credential required; agent API keys cannot call resolve",
        "variable_prefix": CREDENTIAL_PREFIX,
    }

    rate_limits = {
        "memory_write_agent": {"limit": 60, "window": "minute"},
        "memory_search_agent": {"limit": 60, "window": "minute"},
        "credential_create_user": {"limit": 10, "window": "minute"},
        "login_failed_user": {"limit": 10, "window": "minute"},
        "otp_failed_user": {"limit": 5, "window": "5 minutes"},
        "concurrent_search_agent": {"limit": 5, "window": "concurrent"},
    }

    backup_restore = {
        "export_requires": "admin session",
        "restore_modes": ["replace_all", "merge"],
        "replace_all": "Decrypts the uploaded backup archive with the one-time backup key, then wipes current database and encryption key and replaces them with backup contents",
        "merge": "Adds missing records, skips records with conflicting primary keys (no overwrite)",
        "merge_preserves": "All existing records not present in backup remain intact",
        "conflict_behavior": "Primary key collision: existing record wins; backup record is skipped",
        "audit_logged": "backup_restore event written with exported_by and exported_at from manifest",
        "credential_key_handling": "replace_all decrypts the encrypted archive using the backup key; merge preserves the current encryption key and re-encrypts imported credential entries when the backup key differs",
    }

    from app.services.agent_service import is_solo_mode_enabled

    feature_flags = {
        "semantic_search": "hybrid FTS5 + vector similarity when embedding backend and sqlite-vec are available",
        "solo_mode": {
            "enabled": is_solo_mode_enabled(),
            "description": "When enabled, new agents automatically receive user:owner read scope",
        },
        "shared_scope_pii_gate": "shared-scope writes are rejected if PII is detected",
        "supersession_tracking": "Memory records can supersede each other; chains are queryable",
    }

    return success_response(
        {
            "version": "1.0.0",
            "base_url": f"http://localhost:{settings.PORT}",
            "mcp_endpoint": "/mcp",
            "rest_api_prefix": "/api",
            "auth_methods": ["api_key", "session"],
            "session_duration_hours": settings.SESSION_DURATION_HOURS,
            "inactivity_timeout_minutes": settings.INACTIVITY_TIMEOUT_MINUTES,
            "scope_model": scope_model,
            "rest_endpoints": rest_endpoints,
            "mcp_tools": mcp_tools,
            "broker_behavior": broker_behavior,
            "rate_limits": rate_limits,
            "backup_restore": backup_restore,
            "feature_flags": feature_flags,
        }
    )


@router.get("/spec/public")
def spec_public():
    return success_response(
        {
            "version": "1.0.0",
            "mcp_endpoint": "/mcp",
            "auth_methods": ["api_key", "session"],
        }
    )
