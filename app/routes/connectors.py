import json
import time
import threading
from fastapi import APIRouter, Depends
from pydantic import BaseModel, ConfigDict, Field
from typing import Optional

from app.services import (
    connector_service,
    credential_service,
    audit_service,
    openapi_service,
    mcp_provider_service,
)
from app.security.dependencies import get_request_context
from app.security.context import RequestContext
from app.security.scope_enforcer import ScopeEnforcer
from app.security.response_helpers import success_response, error_response


router = APIRouter(prefix="/api/connector-bindings", tags=["connector_bindings"])


class ImportSpecRequest(BaseModel):
    url: Optional[str] = None
    spec_json: Optional[str] = None
    display_name: Optional[str] = None


class ImportMcpServerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    url: str
    display_name: Optional[str] = None
    transport_type: str = "streamable_http"
    timeout_ms: int = 60000
    headers_json: Optional[str] = None

    @staticmethod
    def allowed_transports() -> set[str]:
        return {"streamable_http", "http"}


class RefreshMcpServerRequest(BaseModel):
    timeout_ms: int = 60000
    headers_json: Optional[str] = None


class CreateBindingRequest(BaseModel):
    connector_type_id: str
    name: str
    scope: str
    credential_id: Optional[str] = None
    config_json: Optional[str] = None
    enabled: bool = True


class UpdateBindingRequest(BaseModel):
    name: Optional[str] = None
    scope: Optional[str] = None
    credential_id: Optional[str] = None
    config_json: Optional[str] = None
    enabled: Optional[bool] = None


class ActionSettingsRequest(BaseModel):
    disabled_actions: list[str] = Field(default_factory=list)


@router.get("")
async def list_bindings(
    scope: Optional[str] = None,
    connector_type_id: Optional[str] = None,
    enabled: Optional[bool] = None,
    ctx: RequestContext = Depends(get_request_context),
):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if scope and not enforcer.can_read(scope):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
    bindings = connector_service.list_bindings(
        scope=scope,
        connector_type_id=connector_type_id,
        enabled=enabled,
    )
    allowed = [b for b in bindings if enforcer.can_read(b["scope"])]
    return success_response({"bindings": allowed, "total": len(allowed)})


@router.post("")
async def create_binding(
    body: CreateBindingRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(body.scope):
        return error_response("SCOPE_DENIED", "Access denied to this scope", 403)
    ct = connector_service.get_connector_type(body.connector_type_id)
    if not ct:
        return error_response("NOT_FOUND", "Connector type not found", 404)
    if body.credential_id:
        credential = credential_service.get_credential(body.credential_id)
        if not credential:
            return error_response("NOT_FOUND", "Credential not found", 404)
        if not enforcer.can_read(credential["scope"]):
            return error_response(
                "SCOPE_DENIED", "Access denied to linked credential", 403
            )
    try:
        binding = connector_service.create_binding(
            connector_type_id=body.connector_type_id,
            name=body.name,
            scope=body.scope,
            credential_id=body.credential_id,
            config_json=body.config_json,
            enabled=body.enabled,
            created_by=ctx.user_id,
        )
    except ValueError as e:
        return error_response("INVALID_CONFIG", str(e), 400)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_binding_created",
        resource_type="connector_binding",
        resource_id=binding["id"],
        result="success",
    )
    return success_response({"binding": binding}, status_code=201)


@router.get("/{binding_id}")
async def get_binding(
    binding_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    return success_response({"binding": binding})


@router.put("/{binding_id}")
async def update_binding(
    binding_id: str,
    body: UpdateBindingRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    if body.scope and not enforcer.can_write(body.scope):
        return error_response("SCOPE_DENIED", "Access denied to new scope", 403)
    if body.credential_id:
        credential = credential_service.get_credential(body.credential_id)
        if not credential:
            return error_response("NOT_FOUND", "Credential not found", 404)
        if not enforcer.can_read(credential["scope"]):
            return error_response(
                "SCOPE_DENIED", "Access denied to linked credential", 403
            )
    try:
        ok = connector_service.update_binding(
            binding_id,
            name=body.name,
            scope=body.scope,
            credential_id=body.credential_id,
            config_json=body.config_json,
            enabled=body.enabled,
        )
    except ValueError as e:
        return error_response("INVALID_CONFIG", str(e), 400)
    if not ok:
        return error_response("UPDATE_FAILED", "No valid fields to update", 400)
    return success_response({"binding": connector_service.get_binding(binding_id)})


@router.delete("/{binding_id}")
async def delete_binding(
    binding_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_write(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    connector_service.delete_binding(binding_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_binding_deleted",
        resource_type="connector_binding",
        resource_id=binding_id,
        result="success",
    )
    return success_response({"message": "Binding deleted"})


@router.post("/{binding_id}/test")
async def test_binding(
    binding_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    result = connector_service.test_binding(binding_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_binding_tested",
        resource_type="connector_binding",
        resource_id=binding_id,
        result=result.get("success") and "success" or "failure",
    )
    return success_response({"result": result})


@router.post("/{binding_id}/run")
async def run_binding(
    binding_id: str,
    body: dict,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    action = body.get("action")
    params = body.get("params") or {}
    if not action:
        return error_response("INVALID_REQUEST", "Missing action", 400)
    result = connector_service.execute_binding_action_with_logging(
        binding_id, action, params
    )
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_action_executed",
        resource_type="connector_binding",
        resource_id=binding_id,
        result=result.get("success") and "success" or "failure",
        details={
            "connector_type_id": binding["connector_type_id"],
            "action": action,
            "transport": result.get("transport"),
        },
    )
    return success_response({"result": result})


@router.get("/{binding_id}/executions")
async def list_binding_executions(
    binding_id: str,
    limit: int = 50,
    offset: int = 0,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)
    executions = connector_service.list_executions(
        binding_id, limit=limit, offset=offset
    )
    return success_response({"executions": executions, "total": len(executions)})


@router.get("/{binding_id}/tools")
async def get_binding_tools(
    binding_id: str,
    query: Optional[str] = None,
    include_disabled: bool = False,
    limit: int = 20,
    offset: int = 0,
    ctx: RequestContext = Depends(get_request_context),
):
    binding = connector_service.get_binding(binding_id)
    if not binding:
        return error_response("NOT_FOUND", "Binding not found", 404)
    enforcer = ScopeEnforcer(
        ctx.read_scopes,
        ctx.write_scopes,
        ctx.agent_id,
        is_admin=ctx.is_admin,
        active_workspace_ids=ctx.active_workspace_ids,
    )
    if not enforcer.can_read(binding["scope"]):
        return error_response("SCOPE_DENIED", "Access denied to this binding", 403)

    connector_type = connector_service.get_connector_type(binding["connector_type_id"])
    if not connector_type:
        return success_response({"tools": [], "total": 0})
    result = connector_service.generate_connector_type_tools(
        connector_type,
        disabled_actions=connector_type.get("disabled_actions") or [],
        include_disabled=include_disabled,
        query=query,
        limit=limit,
        offset=offset,
    )
    return success_response(result)


connector_types_router = APIRouter(
    prefix="/api/connector-types", tags=["connector_types"]
)


@connector_types_router.get("")
async def list_connector_types(
    ctx: RequestContext = Depends(get_request_context),
):
    types = connector_service.list_connector_types()
    return success_response({"connector_types": types, "total": len(types)})


@connector_types_router.get("/{connector_type_id}/tools")
async def get_connector_type_tools(
    connector_type_id: str,
    query: Optional[str] = None,
    include_disabled: bool = False,
    limit: int = 20,
    offset: int = 0,
    ctx: RequestContext = Depends(get_request_context),
):
    ct = connector_service.get_connector_type(connector_type_id)
    if not ct:
        return error_response("NOT_FOUND", "Connector type not found", 404)
    result = connector_service.generate_connector_type_tools(
        ct,
        disabled_actions=ct.get("disabled_actions") or [],
        include_disabled=include_disabled,
        query=query,
        limit=limit,
        offset=offset,
    )
    return success_response(result)


@connector_types_router.put("/{connector_type_id}/actions")
async def update_connector_type_actions(
    connector_type_id: str,
    body: ActionSettingsRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required", 403)
    ct = connector_service.get_connector_type(connector_type_id)
    if not ct:
        return error_response("NOT_FOUND", "Connector type not found", 404)

    valid_actions = set(ct.get("supported_actions") or [])
    disabled_actions = []
    for action in body.disabled_actions or []:
        if action not in valid_actions:
            return error_response(
                "INVALID_ACTION",
                f"Unknown action for this connector type: {action}",
                400,
            )
        disabled_actions.append(action)

    connector_service.update_connector_type_actions(connector_type_id, disabled_actions)
    updated = connector_service.get_connector_type(connector_type_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_type_actions_updated",
        resource_type="connector_type",
        resource_id=connector_type_id,
        result="success",
        details={"disabled_actions": disabled_actions},
    )
    return success_response({"connector_type": updated})


_APIS_GURU_URL = "https://api.apis.guru/v2/list.json"
_directory_cache = {"data": None, "fetched_at": 0}
_directory_lock = threading.Lock()
_DIRECTORY_TTL = 3600


def _group_directory_entries(raw_entries: list[dict]) -> list[dict]:
    grouped = {}
    order = []
    for entry in raw_entries:
        key = (entry.get("provider"), entry.get("display_name"))
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(dict(entry))

    entries = []
    for key in order:
        variants = grouped[key]
        variants.sort(key=lambda e: (e.get("id") or "", e.get("version") or ""))
        preferred = variants[0]
        for variant in variants:
            if ":" not in preferred.get("id", "") and ":" in variant.get("id", ""):
                continue
            if ":" in preferred.get("id", "") and ":" not in variant.get("id", ""):
                preferred = variant
        summary = dict(preferred)
        summary["variant_count"] = len(variants)
        summary["variants"] = variants
        entries.append(summary)
    return entries


def _fetch_directory():
    import urllib.request

    now = time.time()
    if (
        _directory_cache["data"]
        and (now - _directory_cache["fetched_at"]) < _DIRECTORY_TTL
    ):
        return _directory_cache["data"]

    try:
        req = urllib.request.Request(
            _APIS_GURU_URL, headers={"User-Agent": "AgentCore/1.0"}
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = json.loads(resp.read().decode())
    except Exception:
        return _directory_cache["data"] or []

    raw_entries = []
    for api_id, versions in raw.items():
        if not isinstance(versions, dict):
            continue
        pref = versions.get("preferred", "")
        ver_data = versions.get("versions", {}).get(pref)
        if not isinstance(ver_data, dict):
            continue
        info = ver_data.get("info", {})
        if not isinstance(info, dict):
            continue
        spec_url = ver_data.get("swaggerUrl") or ver_data.get("swaggerYamlUrl", "")
        if not spec_url:
            continue
        categories = info.get("x-apisguru-categories", [])
        category = categories[0] if categories else "other"
        provider = info.get("x-providerName", api_id.split(":")[0])
        service = info.get("x-serviceName", "")
        logo_url = ""
        logo_info = info.get("x-logo")
        if isinstance(logo_info, dict):
            logo_url = logo_info.get("url", "")
        origin_url = ""
        origins = info.get("x-origin")
        if isinstance(origins, list) and origins:
            origin_url = origins[0].get("url", "")
        raw_entries.append(
            {
                "id": api_id,
                "display_name": info.get("title", api_id),
                "description": (info.get("description") or "")[:500],
                "category": category,
                "categories": categories,
                "spec_url": spec_url,
                "provider": provider,
                "service": service,
                "version": pref,
                "logo_url": logo_url,
                "origin_url": origin_url,
                "website": info.get("contact", {}).get("url", "")
                if isinstance(info.get("contact"), dict)
                else "",
            }
        )

    entries = _group_directory_entries(raw_entries)

    with _directory_lock:
        _directory_cache["data"] = entries
        _directory_cache["fetched_at"] = now

    return entries


@connector_types_router.get("/directory")
async def get_directory(
    q: Optional[str] = None,
    category: Optional[str] = None,
    page: int = 1,
    limit: int = 50,
    ctx: RequestContext = Depends(get_request_context),
):
    entries = _fetch_directory()
    installed_ids = {t["id"] for t in connector_service.list_connector_types()}

    for e in entries:
        e["installed"] = e["id"] in installed_ids
        for v in e.get("variants") or []:
            v["installed"] = v["id"] in installed_ids

    if q:
        ql = q.lower()
        entries = [
            e
            for e in entries
            if ql in e["display_name"].lower()
            or ql in e["description"].lower()
            or ql in e.get("provider", "").lower()
        ]
    if category:
        entries = [
            e for e in entries if e.get("category", "").lower() == category.lower()
        ]

    total = len(entries)
    start = (page - 1) * limit
    page_entries = entries[start : start + limit]

    all_categories = sorted(
        {e["category"] for e in _fetch_directory() if e.get("category")}
    )

    return success_response(
        {
            "entries": page_entries,
            "total": total,
            "page": page,
            "limit": limit,
            "categories": all_categories,
        }
    )


@connector_types_router.post("/import")
async def import_spec(
    body: ImportSpecRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required to import specs", 403)

    if not body.url and not body.spec_json:
        return error_response(
            "INVALID_REQUEST", "Provide either 'url' or 'spec_json'", 400
        )

    try:
        if body.url:
            result = openapi_service.import_spec(
                body.url, display_name=body.display_name, is_url=True
            )
        else:
            result = openapi_service.import_spec(
                body.spec_json, display_name=body.display_name, is_url=False
            )
    except ValueError as e:
        return error_response("IMPORT_FAILED", str(e), 400)
    except Exception as e:
        return error_response("IMPORT_FAILED", f"Unexpected error: {e}", 500)

    existing = connector_service.get_connector_type(result["connector_type_id"])
    if existing:
        connector_service.update_connector_type(
            result["connector_type_id"],
            display_name=result["display_name"],
            description=result["description"],
            auth_type=result["auth_type"],
            supported_actions_json=json.dumps(result["supported_actions"]),
            spec_url=result.get("spec_url"),
            operations_json=result["operations_json"],
            disabled_actions_json=json.dumps(existing.get("disabled_actions") or []),
        )
        ct = connector_service.get_connector_type(result["connector_type_id"])
    else:
        ct = connector_service.create_connector_type(
            connector_type_id=result["connector_type_id"],
            display_name=result["display_name"],
            description=result["description"],
            provider_type="openapi",
            auth_type=result["auth_type"],
            supported_actions=result["supported_actions"],
            spec_url=result.get("spec_url"),
            operations_json=result["operations_json"],
        )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_type_imported",
        resource_type="connector_type",
        resource_id=ct["id"],
        result="success",
    )

    return success_response(
        {
            "connector_type": ct,
            "operation_count": result["operation_count"],
            "warnings": result.get("warnings", []),
        },
        status_code=201,
    )


@connector_types_router.post("/preview")
async def preview_spec(
    body: ImportSpecRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required to preview specs", 403)

    if not body.url and not body.spec_json:
        return error_response(
            "INVALID_REQUEST", "Provide either 'url' or 'spec_json'", 400
        )

    try:
        if body.url:
            result = openapi_service.import_spec(
                body.url, display_name=body.display_name, is_url=True
            )
        else:
            result = openapi_service.import_spec(
                body.spec_json, display_name=body.display_name, is_url=False
            )
    except ValueError as e:
        return error_response("PREVIEW_FAILED", str(e), 400)
    except Exception as e:
        return error_response("PREVIEW_FAILED", f"Unexpected error: {e}", 500)

    preview = {
        "connector_type_id": result["connector_type_id"],
        "display_name": result["display_name"],
        "description": result["description"],
        "auth_type": result["auth_type"],
        "servers": result["servers"],
        "warnings": result.get("warnings", []),
        "operation_count": result["operation_count"],
        "supported_actions": result["supported_actions"],
    }
    return success_response({"preview": preview})


@connector_types_router.post("/import-mcp")
async def import_mcp_server(
    body: ImportMcpServerRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response(
            "FORBIDDEN", "Admin access required to import MCP servers", 403
        )
    if body.transport_type not in ImportMcpServerRequest.allowed_transports():
        return error_response(
            "INVALID_REQUEST",
            f"Unsupported transport_type: {body.transport_type}",
            400,
        )

    try:
        headers = {}
        if body.headers_json:
            parsed_headers = json.loads(body.headers_json)
            if not isinstance(parsed_headers, dict):
                raise ValueError("headers_json must be a JSON object")
            headers = {
                str(k): str(v)
                for k, v in parsed_headers.items()
                if str(k).strip()
            }
        discovery = mcp_provider_service.discover_mcp_server(
            body.url,
            timeout_ms=body.timeout_ms,
            headers=headers or None,
        )
    except json.JSONDecodeError:
        return error_response("INVALID_REQUEST", "headers_json must be valid JSON", 400)
    except ValueError as e:
        return error_response("IMPORT_FAILED", str(e), 400)
    except Exception as e:
        return error_response("IMPORT_FAILED", f"Unexpected error: {e}", 500)

    connector_type_id = openapi_service.generate_connector_id(
        body.display_name or discovery.server_name or body.url
    )
    tool_snapshot = json.dumps(
        {
            "server_info": {
                "name": discovery.server_name,
                "protocol_version": discovery.protocol_version,
            },
            "capabilities": discovery.capabilities,
            "tools": discovery.tools,
        }
    )

    existing = connector_service.get_connector_type(connector_type_id)
    if existing:
        connector_service.update_connector_type(
            connector_type_id,
            display_name=body.display_name or discovery.server_name,
            description=f"MCP server imported from {body.url}",
            provider_type="mcp",
            auth_type="none",
            supported_actions_json=json.dumps([t["name"] for t in discovery.tools]),
            required_credential_fields_json=json.dumps([]),
            disabled_actions_json=json.dumps(existing.get("disabled_actions") or []),
            endpoint_url=mcp_provider_service.validate_mcp_server_url(body.url),
            transport_type=body.transport_type,
            capabilities_json=json.dumps(discovery.capabilities),
            tool_snapshot_json=tool_snapshot,
            spec_url=None,
            operations_json=None,
        )
        ct = connector_service.get_connector_type(connector_type_id)
    else:
        ct = connector_service.create_connector_type(
            connector_type_id=connector_type_id,
            display_name=body.display_name or discovery.server_name,
            description=f"MCP server imported from {body.url}",
            provider_type="mcp",
            auth_type="none",
            supported_actions=[t["name"] for t in discovery.tools],
            required_credential_fields=[],
            endpoint_url=mcp_provider_service.validate_mcp_server_url(body.url),
            transport_type=body.transport_type,
            capabilities_json=json.dumps(discovery.capabilities),
            tool_snapshot_json=tool_snapshot,
        )

    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_type_imported",
        resource_type="connector_type",
        resource_id=ct["id"],
        result="success",
        details={
            "provider_type": "mcp",
            "tool_count": len(discovery.tools),
            "endpoint_url": mcp_provider_service.validate_mcp_server_url(body.url),
            "transport_type": body.transport_type,
        },
    )

    return success_response(
        {
            "connector_type": ct,
            "tool_count": len(discovery.tools),
            "server_name": discovery.server_name,
            "protocol_version": discovery.protocol_version,
            "capabilities": discovery.capabilities,
        },
        status_code=201,
    )


@connector_types_router.post("/{connector_type_id}/refresh")
async def refresh_mcp_connector_type(
    connector_type_id: str,
    body: RefreshMcpServerRequest,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required", 403)
    ct = connector_service.get_connector_type(connector_type_id)
    if not ct:
        return error_response("NOT_FOUND", "Connector type not found", 404)
    if ct.get("provider_type") != "mcp":
        return error_response("INVALID_REQUEST", "Connector type is not an MCP provider", 400)
    endpoint_url = ct.get("endpoint_url")
    if not endpoint_url:
        return error_response("INVALID_REQUEST", "MCP connector has no endpoint_url", 400)

    timeout_ms = int(body.timeout_ms or 60000)
    headers = None
    if body.headers_json:
        try:
            parsed_headers = json.loads(body.headers_json)
            if isinstance(parsed_headers, dict):
                headers = {
                    str(k): str(v) for k, v in parsed_headers.items() if str(k).strip()
                }
        except json.JSONDecodeError:
            return error_response("INVALID_REQUEST", "headers_json must be valid JSON", 400)

    try:
        discovery = mcp_provider_service.discover_mcp_server(
            endpoint_url,
            timeout_ms=timeout_ms,
            headers=headers,
        )
    except ValueError as e:
        return error_response("REFRESH_FAILED", str(e), 400)
    except Exception as e:
        return error_response("REFRESH_FAILED", f"Unexpected error: {e}", 500)

    tool_snapshot = json.dumps(
        {
            "server_info": {
                "name": discovery.server_name,
                "protocol_version": discovery.protocol_version,
            },
            "capabilities": discovery.capabilities,
            "tools": discovery.tools,
        }
    )
    ok = connector_service.update_connector_type(
        connector_type_id,
        display_name=ct["display_name"],
        description=ct.get("description"),
        provider_type="mcp",
        auth_type=ct.get("auth_type") or "none",
        supported_actions_json=json.dumps([t["name"] for t in discovery.tools]),
        required_credential_fields_json=json.dumps(
            ct.get("required_credential_fields") or []
        ),
        disabled_actions_json=json.dumps(ct.get("disabled_actions") or []),
        endpoint_url=endpoint_url,
        transport_type=ct.get("transport_type") or "streamable_http",
        capabilities_json=json.dumps(discovery.capabilities),
        tool_snapshot_json=tool_snapshot,
        spec_url=None,
        operations_json=None,
    )
    if not ok:
        return error_response("REFRESH_FAILED", "Unable to update MCP connector type", 500)

    refreshed = connector_service.get_connector_type(connector_type_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_type_refreshed",
        resource_type="connector_type",
        resource_id=connector_type_id,
        result="success",
        details={
            "provider_type": "mcp",
            "tool_count": len(discovery.tools),
            "endpoint_url": endpoint_url,
            "transport_type": refreshed.get("transport_type") if refreshed else ct.get("transport_type"),
        },
    )
    return success_response(
        {
            "connector_type": refreshed,
            "tool_count": len(discovery.tools),
            "server_name": discovery.server_name,
            "protocol_version": discovery.protocol_version,
            "capabilities": discovery.capabilities,
        }
    )


@connector_types_router.delete("/{connector_type_id}")
async def delete_connector_type(
    connector_type_id: str,
    ctx: RequestContext = Depends(get_request_context),
):
    if not ctx.is_admin:
        return error_response("FORBIDDEN", "Admin access required", 403)
    ct = connector_service.get_connector_type(connector_type_id)
    if not ct:
        return error_response("NOT_FOUND", "Connector type not found", 404)
    connector_service.delete_connector_type(connector_type_id)
    audit_service.write_event(
        actor_type=ctx.actor_type,
        actor_id=ctx.actor_id,
        action="connector_type_deleted",
        resource_type="connector_type",
        resource_id=connector_type_id,
        result="success",
    )
    return success_response({"message": "Connector type deleted"})
