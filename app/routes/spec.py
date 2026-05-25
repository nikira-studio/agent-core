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
async def spec(
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
            "description": "Search memory records by text query within authorized scopes",
            "inputSchema": {
                "query": "string",
                "domain": "string?",
                "topic": "string?",
                "memory_class": list(MEMORY_CLASSES),
                "min_confidence": "number?",
                "limit": "integer?",
                "include_retracted": "boolean?",
                "include_superseded": "boolean?",
            },
        },
        {
            "name": "memory_get",
            "description": "Get memory records by scope or list active records",
            "inputSchema": {
                "scope": "string?",
                "record_status": "string?",
                "limit": "integer?",
            },
        },
        {
            "name": "memory_write",
            "description": "Write a new memory record",
            "inputSchema": {
                "content": "string",
                "memory_class": list(MEMORY_CLASSES),
                "scope": "string",
                "domain": "string?",
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
            "description": "Update the current agent's active activity or create one if none exists",
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
            "name": "connectors_list",
            "description": "List available connector types",
            "inputSchema": {},
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
async def spec_public():
    return success_response(
        {
            "version": "1.0.0",
            "mcp_endpoint": "/mcp",
            "auth_methods": ["api_key", "session"],
        }
    )
