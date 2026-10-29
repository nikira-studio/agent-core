from fastapi import APIRouter, Depends

from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.response_helpers import success_response
from app.branding import CREDENTIAL_PREFIX
from app.config import settings
from app.models.enums import MEMORY_CLASSES, SOURCE_KINDS


router = APIRouter()


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
        {"prefix": "/api/credential-consumers", "methods": ["POST", "GET", "DELETE"], "description": "Human-admin exact-entry grants and HTTPS agent-key resolve/status; explicit credentials_resolve capability"},
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
            "description": "Get memory records by id (ids), by scope, or list active records; view='compact' surveys a scope (metadata + preview), defaults compact for large pages",
            "inputSchema": {
                "ids": "string[]?",
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
            "name": "credential_request",
            "description": "Ask a person to supply a secret on the Credentials page; returns the AC_SECRET reference once filled",
            "inputSchema": {"scope": "string", "name": "string", "purpose": "string"},
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
                "execution_id": "string?",
                "handoff": "object?",
            },
        },
        {
            "name": "activity_handoff_resolve",
            "description": "Resolve one of your own handoff requests to a person once it has been dealt with",
            "inputSchema": {"activity_id": "string", "reason": "string"},
        },
        {
            "name": "activity_get",
            "description": "Get a specific activity by ID",
            "inputSchema": {"activity_id": "string"},
        },
        {
            "name": "activity_list",
            "description": "List activities visible to the current agent or user, as compact previews unless view=full",
            "inputSchema": {
                "status": "string?",
                "agent_id": "string?",
                "assigned_agent_id": "string?",
                "limit": "integer?",
                "offset": "integer?",
                "view": "compact|full?",
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
                "view": "compact|full?",
            },
        },
        {
            "name": "attention_list",
            "description": "Unresolved needs_human handoffs across readable scopes, newest first; read-only, for coordinators",
            "inputSchema": {"scopes": "string[]?", "without_task_ref": "boolean?", "limit": "integer?"},
        },
        {
            "name": "activity_pickup",
            "description": "Claim the next active work item assigned to this agent in authorized scopes",
            "inputSchema": {},
        },
        {
            "name": "workspace_sync",
            "description": "Synchronize workspace context and changes for one agent execution",
            "inputSchema": {
                "memory_scope": "string",
                "execution_id": "string?",
                "after_cursor": "integer?",
                "limit": "integer?",
            },
        },
        {
            "name": "session_context",
            "description": "One budgeted session-start block: standing rules, unfinished and handed-off work, recent results, changes since your last session, an index of key records, and the execution_id",
            "inputSchema": {
                "memory_scope": "string?",
                "cwd": "string?",
                "host_session_ref": "string?",
                "max_chars": "integer?",
            },
        },
        {
            "name": "workspace_sync_ack",
            "description": "Acknowledge a delivered workspace synchronization cursor",
            "inputSchema": {
                "memory_scope": "string",
                "execution_id": "string",
                "cursor": "integer",
            },
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
            "name": "effective_authority",
            "description": "Inspect non-secret effective authority",
            "inputSchema": {},
        },
        {
            "name": "connectors_summary",
            "description": "Summarize visible connector types and bindings: usable, health and failing actions per binding; view='full' adds credential and health detail",
            "inputSchema": {"scope": "string?", "connector_type_id": "string?", "enabled_only": "boolean?", "view": "string?"},
        },
        {
            "name": "connectors_actions_list",
            "description": "List a connector type's actions compactly; pass action for one action's full input schema",
            "inputSchema": {"connector_type_id": "string", "action": "string?", "query": "string?", "view": "string?", "limit": "integer?", "offset": "integer?"},
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
        "restore_modes": ["replace_all"],
        "replace_all": "Decrypts the uploaded backup archive with the one-time backup key, then wipes current database and encryption key and replaces them with backup contents",
        "audit_logged": "backup_restore event written with exported_by and exported_at from manifest",
        "credential_key_handling": "replace_all decrypts the encrypted archive using the backup key; when AGENT_CORE_ENCRYPTION_KEY is configured, restored credential entries are re-encrypted under that key",
    }

    feature_flags = {
        "semantic_search": "hybrid FTS5 + vector similarity when embedding backend and sqlite-vec are available",
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
