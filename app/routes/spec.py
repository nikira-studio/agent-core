from fastapi import APIRouter, Depends
from pydantic import BaseModel
from typing import Optional

from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.response_helpers import success_response
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
        "description": "Agents access memory and vault through scopes: agent:<id>, user:<id>, workspace:<id>, shared",
        "read_scopes": "Scopes the principal can read from",
        "write_scopes": "Scopes the principal can write to",
        "shared": "Broad shared scope; shared write requires explicit grant",
        "scope_ceiling": "Maximum scopes an agent can access; agents belong to one owner/default user and use workspace:<id> workspaces for collaboration",
    }

    rest_endpoints = [
        {"prefix": "/api/auth", "methods": ["POST"], "description": "Login, register, OTP verify, logout"},
        {"prefix": "/api/memory", "methods": ["POST", "GET"], "description": "Memory write, search, get, retract, detail"},
        {"prefix": "/api/vault", "methods": ["POST", "GET"], "description": "Vault entry create, list, reveal (OTP required)"},
        {"prefix": "/api/agents", "methods": ["POST", "GET"], "description": "Agent create, list, rotate key, deactivate"},
        {"prefix": "/api/workspaces", "methods": ["POST", "GET"], "description": "Workspace CRUD"},
        {"prefix": "/api/activity", "methods": ["POST", "GET"], "description": "Activity create, list, update, heartbeat"},
        {"prefix": "/api/briefings", "methods": ["POST", "GET"], "description": "Handoff briefing generate and retrieve"},
        {"prefix": "/api/backup", "methods": ["GET", "POST"], "description": "Backup export/restore, partial exports"},
        {"prefix": "/mcp", "methods": ["GET", "POST"], "description": "MCP manifest and tool calls"},
    ]

    mcp_tools = [
        {"name": "memory_search", "description": "Search memory records by text query within authorized scopes", "inputSchema": {"query": "string", "domain": "string?", "topic": "string?", "memory_class": list(MEMORY_CLASSES), "min_confidence": "number?", "limit": "integer?", "include_retracted": "boolean?", "include_superseded": "boolean?"}},
        {"name": "memory_get", "description": "Get memory records by scope or list active records", "inputSchema": {"scope": "string?", "record_status": "string?", "limit": "integer?"}},
        {"name": "memory_write", "description": "Write a new memory record", "inputSchema": {"content": "string", "memory_class": list(MEMORY_CLASSES), "scope": "string", "domain": "string?", "topic": "string?", "confidence": "number?", "importance": "number?", "source_kind": list(SOURCE_KINDS), "supersedes_id": "string?"}},
        {"name": "memory_retract", "description": "Retract a memory record by ID", "inputSchema": {"record_id": "string"}},
        {"name": "vault_get", "description": "Get a vault credential reference name by entry ID", "inputSchema": {"entry_id": "string"}},
        {"name": "vault_list", "description": "List vault credential references in authorized scopes", "inputSchema": {"scope": "string?", "limit": "integer?"}},
        {"name": "activity_update", "description": "Update the current agent's active activity or create one if none exists", "inputSchema": {"task_description": "string?", "status": "string?", "memory_scope": "string?"}},
        {"name": "activity_get", "description": "Get a specific activity by ID", "inputSchema": {"activity_id": "string"}},
        {"name": "get_briefing", "description": "Get a handoff briefing by ID", "inputSchema": {"briefing_id": "string"}},
    ]

    broker_behavior = {
        "description": "Credential values are resolved internally by the credential broker and never exposed in API responses, prompts, or logs",
        "resolve_endpoint": "/internal/vault/resolve",
        "resolve_auth": "broker credential required; agent API keys cannot call resolve",
        "variable_prefix": "AC_SECRET_",
    }

    rate_limits = {
        "memory_write_agent": {"limit": 60, "window": "minute"},
        "memory_search_agent": {"limit": 60, "window": "minute"},
        "vault_create_user": {"limit": 10, "window": "minute"},
        "login_failed_user": {"limit": 10, "window": "minute"},
        "otp_failed_user": {"limit": 5, "window": "5 minutes"},
        "concurrent_search_agent": {"limit": 5, "window": "concurrent"},
    }

    backup_restore = {
        "export_requires": "admin session + OTP verification",
        "restore_modes": ["replace_all", "merge"],
        "replace_all": "Wipes current database and vault key, replaces with backup contents",
        "merge": "Adds missing records, skips records with conflicting primary keys (no overwrite)",
        "merge_preserves": "All existing records not present in backup remain intact",
        "conflict_behavior": "Primary key collision: existing record wins; backup record is skipped",
        "audit_logged": "backup_restore event written with exported_by and exported_at from manifest",
        "vault_key_handling": "replace_all uses the backup vault.key; merge preserves the current vault.key and re-encrypts imported vault entries when the backup key differs",
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

    return success_response({
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
    })


@router.get("/spec/public")
async def spec_public():
    return success_response({
        "version": "1.0.0",
        "mcp_endpoint": "/mcp",
        "auth_methods": ["api_key", "session"],
    })
