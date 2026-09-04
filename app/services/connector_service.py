import json
import hashlib
import logging
import random
import re
import secrets
from contextvars import ContextVar
import time
from types import SimpleNamespace
from typing import Optional
from app.database import get_db
from app.models.enums import normalize_id
from app.security.exceptions import APIError
from app.services import mcp_provider_service

logger = logging.getLogger(__name__)
CAPABILITY_POLICY_FIELDS = (
    "domain", "category", "risk_level", "idempotent", "approval_required",
    "expected_latency", "background_execution", "data_sensitivity",
    "event_producing", "purpose", "tags",
)
_execution_authority: ContextVar[object | None] = ContextVar(
    "connector_execution_authority", default=None
)


def normalize_action_names(actions: Optional[list]) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for action in actions or []:
        if isinstance(action, str):
            name = action
        elif isinstance(action, dict):
            name = action.get("name")
        else:
            name = None
        if name and name not in seen:
            names.append(name)
            seen.add(name)
    return names


def capability_policy(action: dict, override: dict | None = None) -> dict:
    """Return advisory generic metadata; it never weakens authorization."""
    result = {key: action[key] for key in CAPABILITY_POLICY_FIELDS if key in action}
    if override:
        result.update({key: override[key] for key in CAPABILITY_POLICY_FIELDS if key in override})
    return result


def _capability_policy_overrides(connector_type: dict) -> dict:
    try:
        overrides = json.loads(connector_type.get("capability_policy_overrides_json") or "{}")
    except json.JSONDecodeError:
        return {}
    return overrides if isinstance(overrides, dict) else {}


def capability_policy_override(connector_type: dict, action: str) -> dict:
    value = _capability_policy_overrides(connector_type).get(action)
    return value if isinstance(value, dict) else {}


def _with_capability_policy(connector_type: dict, tool: dict) -> dict:
    action = tool.get("action") or tool.get("name") or ""
    result = dict(tool)
    result["capability_policy"] = capability_policy(
        _action_meta(connector_type, action),
        capability_policy_override(connector_type, action),
    )
    return result


def list_connector_types(include_inactive: bool = False) -> list[dict]:
    """Return the installed service catalog from SQLite.

    Adapter templates are a separate filesystem-backed browse catalog.  A
    connector type already represents an installed service, so checking every
    adapter manifest here adds no information and makes ordinary catalog reads
    CPU-bound.
    """
    with get_db() as conn:
        query = """
            SELECT id, display_name, description, provider_type, auth_type,
                   supported_actions_json, required_credential_fields_json,
                   default_binding_rules_json, disabled_actions_json, capability_policy_overrides_json, endpoint_url,
                   transport_type, capabilities_json, tool_snapshot_json, spec_url,
                   operations_json, backend_type, backend_json,
                   is_active, created_at, updated_at
            FROM connector_types
        """
        if not include_inactive:
            query += " WHERE is_active = 1"
        query += " ORDER BY display_name"
        rows = conn.execute(query).fetchall()
        return [_row_to_connector_type(dict(row)) for row in rows]


def get_connector_type(connector_type_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, display_name, description, provider_type, auth_type,
                   supported_actions_json, required_credential_fields_json,
                   default_binding_rules_json, disabled_actions_json, capability_policy_overrides_json, endpoint_url,
                   transport_type, capabilities_json, tool_snapshot_json, spec_url,
                   operations_json, backend_type, backend_json,
                   is_active, created_at, updated_at
            FROM connector_types
            WHERE id = ?
            """,
            (connector_type_id,),
        ).fetchone()
        return _row_to_connector_type(dict(row)) if row else None


def _row_to_connector_type(row: dict) -> dict:
    return {
        "id": row["id"],
        "display_name": row["display_name"],
        "description": row.get("description"),
        "provider_type": row.get("provider_type") or "openapi",
        "auth_type": row["auth_type"],
        "supported_actions": json.loads(row["supported_actions_json"]),
        "required_credential_fields": json.loads(
            row["required_credential_fields_json"]
        ),
        "default_binding_rules": json.loads(row["default_binding_rules_json"])
        if row.get("default_binding_rules_json")
        else None,
        "disabled_actions": json.loads(row["disabled_actions_json"])
        if row.get("disabled_actions_json")
        else [],
        "capability_policy_overrides_json": row.get("capability_policy_overrides_json") or "{}",
        "is_active": bool(row["is_active"]),
        "endpoint_url": row.get("endpoint_url"),
        "transport_type": row.get("transport_type"),
        "capabilities_json": row.get("capabilities_json"),
        "tool_snapshot_json": row.get("tool_snapshot_json"),
        "spec_url": row.get("spec_url"),
        "operations_json": row.get("operations_json"),
        "backend_type": row.get("backend_type"),
        "backend_json": row.get("backend_json"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _row_to_binding(row: dict) -> dict:
    return {
        "id": row["id"],
        "connector_type_id": row["connector_type_id"],
        "connector_display_name": row.get("connector_display_name"),
        "name": row["name"],
        "scope": row["scope"],
        "credential_id": row.get("credential_id"),
        "config_json": row.get("config_json"),
        "rate_limit_config_json": row.get("rate_limit_config_json"),
        "logical_alias": row.get("logical_alias"),
        "is_preferred": bool(row.get("is_preferred")),
        "priority": int(row.get("priority") or 0),
        "description": row.get("description"),
        "metadata_json": row.get("metadata_json"),
        "endpoint_url_override": row.get("endpoint_url_override"),
        "enabled": bool(row["enabled"]),
        "last_tested_at": row.get("last_tested_at"),
        "last_error": row.get("last_error"),
        "created_by": row.get("created_by"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _parse_json_object(value: Optional[str]) -> Optional[dict]:
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def effective_mcp_endpoint(binding: dict, connector_type: dict) -> str:
    """One endpoint-selection rule shared by test, discovery, and execution."""
    if binding.get("endpoint_url_override"):
        return binding["endpoint_url_override"]
    if connector_type.get("backend_type") == "mcp":
        config = _parse_json_object(binding.get("config_json")) or {}
        endpoint = config.get("base_url") or config.get("endpoint_url")
        if isinstance(endpoint, str) and endpoint.strip():
            return endpoint.strip()
    return connector_type.get("endpoint_url") or ""


_NON_STRUCTURAL_SCHEMA_KEYS = {
    "$comment",
    "$id",
    "$schema",
    "description",
    "examples",
    "example",
    "markdownDescription",
    "title",
}
_ORDER_INSENSITIVE_SCHEMA_LIST_KEYS = {
    "allOf",
    "anyOf",
    "enum",
    "oneOf",
    "required",
    "type",
}
_SECURITY_RELEVANT_TOOL_ANNOTATIONS = {
    "destructiveHint",
    "idempotentHint",
    "openWorldHint",
    "readOnlyHint",
}


def _normalize_schema_with_context(value, parent_key: str | None = None):
    if isinstance(value, dict):
        return {
            key: _normalize_schema_with_context(child, key)
            for key, child in sorted(value.items())
            if key not in _NON_STRUCTURAL_SCHEMA_KEYS
        }
    if isinstance(value, list):
        normalized = [_normalize_schema_with_context(child) for child in value]
        if parent_key in _ORDER_INSENSITIVE_SCHEMA_LIST_KEYS:
            return sorted(
                normalized,
                key=lambda item: json.dumps(item, sort_keys=True, separators=(",", ":")),
            )
        return normalized
    return value


def mcp_tool_contract_fingerprint(tools) -> str:
    """Fingerprint the strict v1 native-MCP capability contract.

    Tool names, input schemas, and authority-relevant annotations are retained.
    Descriptions, examples, and response ordering intentionally do not affect a
    deployment's compatibility with the connector type.
    """
    normalized = []
    for tool in tools or []:
        if not isinstance(tool, dict) or not tool.get("name"):
            continue
        raw = tool.get("raw") if isinstance(tool.get("raw"), dict) else tool
        annotations = raw.get("annotations") or tool.get("annotations") or {}
        normalized.append({
            "name": tool["name"],
            "inputSchema": _normalize_schema_with_context(
                tool.get("inputSchema") or tool.get("input_schema") or raw.get("inputSchema") or {}
            ),
            "annotations": {
                key: annotations[key]
                for key in sorted(_SECURITY_RELEVANT_TOOL_ANNOTATIONS)
                if key in annotations
            },
        })
    serialized = json.dumps(
        sorted(normalized, key=lambda item: item["name"]),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _expected_mcp_tools(connector_type: dict) -> list[dict]:
    try:
        snapshot = json.loads(connector_type.get("tool_snapshot_json") or "")
    except json.JSONDecodeError as exc:
        raise ValueError(
            "Connector type has no valid MCP capability contract; refresh or register a separate connector type"
        ) from exc
    if not isinstance(snapshot, dict) or not isinstance(snapshot.get("tools"), list):
        raise ValueError(
            "Connector type has no MCP capability contract; refresh or register a separate connector type"
        )
    return snapshot["tools"]


def _mcp_binding_request_config(
    binding: dict, connector_type: dict, credential=None
) -> tuple[str, dict[str, str], int]:
    endpoint_url = effective_mcp_endpoint(binding, connector_type)
    if not endpoint_url:
        raise ValueError("MCP connector has no endpoint_url")
    request_binding = dict(binding)
    request_binding["endpoint_url"] = endpoint_url
    return mcp_provider_service.build_mcp_request_config(
        request_binding, credential=credential.raw if credential else None
    )


def discover_mcp_binding_tools(
    binding: dict, connector_type: dict, credential=None, *, timeout_cap_ms: int | None = None
) -> list[dict]:
    endpoint_url, headers, timeout_ms = _mcp_binding_request_config(
        binding, connector_type, credential
    )
    if timeout_cap_ms is not None:
        timeout_ms = min(timeout_ms, timeout_cap_ms)
    return mcp_provider_service.discover_all_tools(
        endpoint_url, timeout_ms=timeout_ms, headers=headers
    )


def validate_mcp_binding_contract(
    binding: dict, connector_type: dict, credential=None, *, tools=None
) -> None:
    expected = _expected_mcp_tools(connector_type)
    actual = tools if tools is not None else discover_mcp_binding_tools(
        binding, connector_type, credential
    )
    backend = _parse_json_object(connector_type.get("backend_json")) or {}
    if backend.get("tool_contract") == "names_only":
        expected_names = {tool.get("name") for tool in expected if tool.get("name")}
        actual_names = {tool.get("name") for tool in actual if tool.get("name")}
        if expected_names == actual_names:
            return
        raise ValueError(
            "Endpoint tool names do not match this connector type; update the adapter or register a separate connector type"
        )
    if mcp_tool_contract_fingerprint(actual) != mcp_tool_contract_fingerprint(expected):
        raise ValueError(
            "Endpoint tool contract does not match this connector type; register a separate connector type"
        )


def _validate_endpoint_override(connector_type, endpoint_url, credential_id, config_json):
    if endpoint_url is None or not str(endpoint_url).strip():
        return None
    if not connector_type or connector_type.get("provider_type") != "mcp":
        raise ValueError("endpoint_url_override is only valid for MCP connector types")
    endpoint_url = mcp_provider_service.validate_mcp_server_url(str(endpoint_url).strip())
    credential = None
    if credential_id:
        from app.services import credential_service

        entry = credential_service.get_credential(credential_id)
        if entry:
            credential = credential_service.resolve_reference(entry["reference_name"])
    candidate = {"endpoint_url_override": endpoint_url, "config_json": config_json}
    credential_object = SimpleNamespace(raw=credential) if credential else None
    discovered = discover_mcp_binding_tools(
        candidate, connector_type, credential_object, timeout_cap_ms=10000
    )
    validate_mcp_binding_contract(
        candidate, connector_type, credential_object, tools=discovered
    )
    return endpoint_url


def list_bindings(
    scope: Optional[str] = None,
    connector_type_id: Optional[str] = None,
    enabled: Optional[bool] = None,
) -> list[dict]:
    with get_db() as conn:
        query = "SELECT cb.*, ct.display_name as connector_display_name FROM connector_bindings cb JOIN connector_types ct ON cb.connector_type_id = ct.id WHERE 1=1"
        params = []
        if scope:
            query += " AND cb.scope = ?"
            params.append(scope)
        if connector_type_id:
            query += " AND cb.connector_type_id = ?"
            params.append(connector_type_id)
        if enabled is not None:
            query += " AND cb.enabled = ?"
            params.append(1 if enabled else 0)
        query += " ORDER BY cb.created_at DESC"
        rows = conn.execute(query, params).fetchall()
        return [_row_to_binding(dict(row)) for row in rows]


def get_binding(binding_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            "SELECT cb.*, ct.display_name as connector_display_name FROM connector_bindings cb JOIN connector_types ct ON cb.connector_type_id = ct.id WHERE cb.id = ?",
            (binding_id,),
        ).fetchone()
        return _row_to_binding(dict(row)) if row else None


def get_binding_with_credential(binding_id: str) -> Optional[dict]:
    binding = get_binding(binding_id)
    if not binding:
        return None
    binding["credential"] = None
    binding["credential_plaintext"] = None
    if binding.get("credential_id"):
        from app.services import credential_service

        cred = credential_service.get_credential(binding["credential_id"])
        if cred:
            from app.services.credential_service import (
                resolve_reference,
                resolve_credential,
            )

            binding["credential_plaintext"] = resolve_reference(cred["reference_name"])
            binding["credential"] = resolve_credential(cred["reference_name"])
    return binding


def create_binding(
    connector_type_id: str,
    name: str,
    scope: str,
    credential_id: Optional[str] = None,
    config_json: Optional[str] = None,
    enabled: bool = True,
    created_by: Optional[str] = None,
    logical_alias: Optional[str] = None,
    is_preferred: bool = False,
    priority: int = 0,
    description: Optional[str] = None,
    metadata_json: Optional[str] = None,
    endpoint_url_override: Optional[str] = None,
) -> dict:
    normalized_scope = _normalize_scope(scope)
    config_data = _parse_json_object(config_json)
    if config_json is not None and config_data is None:
        raise ValueError("config_json must be a JSON object")
    config_json = json.dumps(config_data) if config_data is not None else None
    metadata_data = _parse_json_object(metadata_json)
    if metadata_json is not None and metadata_data is None:
        raise ValueError("metadata_json must be a JSON object")
    metadata_json = json.dumps(metadata_data) if metadata_data is not None else None
    connector_type = get_connector_type(connector_type_id)
    endpoint_url_override = _validate_endpoint_override(
        connector_type, endpoint_url_override, credential_id, config_json
    )
    binding_id = secrets.token_urlsafe(16)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_bindings
            (id, connector_type_id, name, scope, credential_id, config_json, enabled, created_by,
             logical_alias, is_preferred, priority, description, metadata_json, endpoint_url_override)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                binding_id,
                connector_type_id,
                name,
                normalized_scope,
                credential_id,
                config_json,
                1 if enabled else 0,
                created_by,
                logical_alias.strip() if logical_alias and logical_alias.strip() else None,
                int(is_preferred), int(priority), description, metadata_json,
                endpoint_url_override,
            ),
        )
        conn.commit()
    return get_binding(binding_id)


def update_binding(binding_id: str, **fields) -> bool:
    allowed = (
        "name",
        "scope",
        "credential_id",
        "config_json",
        "rate_limit_config_json",
        "enabled",
        "last_tested_at",
        "last_error",
        "logical_alias",
        "is_preferred",
        "priority",
        "description",
        "metadata_json",
        "endpoint_url_override",
    )
    # Status fields are nullable and must be clearable: a successful test has to
    # reset last_error to NULL, otherwise a binding that failed once would keep
    # reporting an error forever. Other fields keep the "skip None" semantics.
    nullable = ("last_tested_at", "last_error", "endpoint_url_override")
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed and (val is not None or key in nullable):
            if key in ("enabled", "is_preferred"):
                updates.append(f"{key} = ?")
                params.append(1 if val else 0)
            elif key == "scope":
                updates.append("scope = ?")
                params.append(_normalize_scope(val))
            elif key == "config_json":
                config_data = _parse_json_object(val)
                if val is not None and config_data is None:
                    raise ValueError("config_json must be a JSON object")
                updates.append("config_json = ?")
                params.append(
                    json.dumps(config_data) if config_data is not None else None
                )
            elif key == "rate_limit_config_json":
                rate_limit_data = _parse_json_object(val)
                if val is not None and rate_limit_data is None:
                    raise ValueError("rate_limit_config_json must be a JSON object")
                updates.append("rate_limit_config_json = ?")
                params.append(
                    json.dumps(rate_limit_data) if rate_limit_data is not None else None
                )
            elif key == "metadata_json":
                metadata_data = _parse_json_object(val)
                if val is not None and metadata_data is None:
                    raise ValueError("metadata_json must be a JSON object")
                updates.append("metadata_json = ?")
                params.append(json.dumps(metadata_data) if metadata_data is not None else None)
            elif key == "endpoint_url_override":
                current = get_binding(binding_id)
                connector_type = get_connector_type(current["connector_type_id"]) if current else None
                credential_id = fields.get("credential_id") if fields.get("credential_id") is not None else current.get("credential_id") if current else None
                config_value = fields.get("config_json") if fields.get("config_json") is not None else current.get("config_json") if current else None
                updates.append("endpoint_url_override = ?")
                params.append(_validate_endpoint_override(connector_type, val, credential_id, config_value))
            elif key == "logical_alias":
                updates.append("logical_alias = ?")
                params.append(val.strip() if isinstance(val, str) and val.strip() else None)
            else:
                updates.append(f"{key} = ?")
                params.append(val)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(binding_id)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE connector_bindings SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def resolve_authorized_binding(
    authority,
    *,
    connector_type_id: str,
    logical_alias: str | None = None,
    scope: str | None = None,
    action: str | None = None,
) -> dict:
    """Resolve one authorized binding deterministically; ambiguity never guesses."""
    normalized_scope = _normalize_scope(scope) if scope else None
    candidates = list_bindings(
        scope=normalized_scope, connector_type_id=connector_type_id, enabled=True
    )
    connector_type = get_connector_type(connector_type_id)
    available_actions = set(normalize_action_names((connector_type or {}).get("supported_actions"))) - set((connector_type or {}).get("disabled_actions") or [])
    visible = []
    for binding in candidates:
        if action and action not in available_actions:
            continue
        if authority.is_delegated:
            allowed = (
                authority.can_binding_action(binding["id"], action, scope=binding["scope"])
                if action else any(item[0] == binding["id"] for item in authority.binding_actions)
            )
        else:
            allowed = (
                can_run_binding_action(authority, binding, connector_type or {}, action)
                if action else authority.can("connector", "read", scope=binding["scope"])
            )
        if allowed:
            visible.append(binding)
    if logical_alias is not None:
        matches = [item for item in visible if item.get("logical_alias") == logical_alias]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise APIError("BINDING_NOT_FOUND", "No authorized binding matches the requested alias", 404)
        visible = matches
    if len(visible) == 1:
        return visible[0]
    preferred = [item for item in visible if item.get("is_preferred")]
    if len(preferred) == 1:
        return preferred[0]
    if visible:
        highest = max(item.get("priority", 0) for item in visible)
        ranked = [item for item in visible if item.get("priority", 0) == highest]
        if len(ranked) == 1:
            return ranked[0]
    raise APIError("AMBIGUOUS_BINDING", "Authorized binding selection is ambiguous", 409)


def delete_binding(binding_id: str) -> bool:
    with get_db() as conn:
        conn.execute(
            """UPDATE delegation_requests SET grant_id = NULL WHERE grant_id IN
               (SELECT grant_id FROM delegated_grant_actions WHERE binding_id = ?)""",
            (binding_id,),
        )
        conn.execute(
            "DELETE FROM delegated_grants WHERE id IN (SELECT grant_id FROM delegated_grant_actions WHERE binding_id = ?)",
            (binding_id,),
        )
        conn.execute(
            "DELETE FROM connector_executions WHERE binding_id = ?", (binding_id,)
        )
        conn.execute(
            "DELETE FROM connector_oauth_states WHERE binding_id = ?", (binding_id,)
        )
        cursor = conn.execute(
            "DELETE FROM connector_bindings WHERE id = ?", (binding_id,)
        )
        conn.commit()
        result = cursor.rowcount > 0
    from app.services import connector_session_service as _css

    _css.clear_session(binding_id)
    return result


_NON_TRANSIENT_CODES = frozenset(
    {
        "NOT_FOUND",
        "DISABLED",
        "INVALID_ACTION",
        "NO_CREDENTIAL",
        "RATE_LIMITED",
        "INVALID_CONFIGURATION",
        "SCOPE_DENIED",
    }
)


def _is_transient_result(result: dict) -> bool:
    if result.get("success"):
        return False
    error_code = result.get("error_code") or ""
    if error_code in _NON_TRANSIENT_CODES:
        return False
    status = result.get("status")
    if isinstance(status, int):
        return status == 429 or status >= 500
    if error_code == "EXECUTION_ERROR":
        msg = (result.get("error") or "").lower()
        return "timeout" in msg or "connection" in msg or "unavailable" in msg
    return False


def _check_rate_limit(binding: dict) -> Optional[str]:
    config = None
    if binding.get("rate_limit_config_json"):
        try:
            config = json.loads(binding["rate_limit_config_json"])
        except json.JSONDecodeError:
            return None
    if not config:
        return None

    min_interval_ms = config.get("min_interval_ms", 0)
    burst = config.get("burst", 0)
    if not min_interval_ms and not burst:
        return None

    with get_db() as conn:
        recent = conn.execute(
            "SELECT executed_at FROM connector_executions WHERE binding_id = ? ORDER BY executed_at DESC LIMIT ?",
            (binding["id"], burst or 1),
        ).fetchall()

    if min_interval_ms and recent:
        from app.time_utils import utc_now, parse_utc_datetime

        last = recent[0]["executed_at"] if recent else None
        if last:
            try:
                last_dt = parse_utc_datetime(last)
                now_dt = utc_now()
                elapsed_ms = int((now_dt - last_dt).total_seconds() * 1000)
                if elapsed_ms < min_interval_ms:
                    return f"Rate limited: retry after {min_interval_ms - elapsed_ms}ms"
            except (ValueError, TypeError):
                pass

    return None


def _resolve_executor(connector_type: dict):
    from app.connectors import resolve_connector

    return resolve_connector(connector_type)


def _build_executor_config(binding: dict, connector_type: dict) -> str:
    config = {}
    if binding.get("config_json"):
        try:
            config = json.loads(binding["config_json"])
        except json.JSONDecodeError:
            pass
    if connector_type.get("operations_json"):
        try:
            config["_operations_json"] = json.loads(connector_type["operations_json"])
        except json.JSONDecodeError:
            pass
    if connector_type.get("backend_type") == "generic_http" or connector_type.get(
        "provider_type"
    ) == "generic_http":
        config.setdefault("base_url", connector_type.get("endpoint_url"))
    return json.dumps(config) if config else None


def _connector_requires_credential(connector_type: dict, config: dict | None = None) -> bool:
    if connector_type.get("auth_type") == "none":
        return False
    if isinstance(config, dict) and config.get("auth_mode") == "none":
        return False
    required_fields = connector_type.get("required_credential_fields")
    if isinstance(required_fields, list):
        return bool(required_fields)
    return connector_type.get("auth_type") != "none"


def _mcp_tools_from_snapshot(
    connector_type_id: str,
    snapshot_json: Optional[str],
    disabled_actions: Optional[list[str]] = None,
    include_disabled: bool = False,
    query: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    try:
        meta = json.loads(snapshot_json or "{}")
    except json.JSONDecodeError:
        return {"tools": [], "total": 0}

    disabled_set = {
        action for action in (disabled_actions or []) if isinstance(action, str)
    }
    tools_meta = meta.get("tools", [])
    all_tools = []
    for tool in tools_meta:
        if not isinstance(tool, dict):
            continue
        name = tool.get("name") or tool.get("tool") or tool.get("action")
        if not name:
            continue
        all_tools.append(
            {
                "name": name,
                "action": name,
                "method": "MCP",
                "path": tool.get("path", ""),
                "summary": tool.get("summary") or tool.get("name", ""),
                "description": tool.get("description", ""),
                "parameters": tool.get("input_schema", {}).get("properties", {})
                if isinstance(tool.get("input_schema"), dict)
                else {},
                "enabled": name not in disabled_set,
            }
        )

    if query:
        q = query.lower()
        all_tools = [
            tool
            for tool in all_tools
            if q in tool["name"].lower()
            or q in (tool.get("summary") or "").lower()
            or q in (tool.get("description") or "").lower()
        ]

    if not include_disabled:
        all_tools = [tool for tool in all_tools if tool["enabled"]]

    total = len(all_tools)
    page = all_tools[offset : offset + limit]
    return {
        "connector_type_id": connector_type_id,
        "tools": page,
        "total": total,
        "provider_type": "mcp",
    }


def generate_connector_type_tools(
    connector_type: dict,
    disabled_actions: Optional[list[str]] = None,
    include_disabled: bool = False,
    query: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    def enrich(result: dict) -> dict:
        result = dict(result)
        result["tools"] = [
            _with_capability_policy(connector_type, tool)
            for tool in result.get("tools", [])
        ]
        return result

    if connector_type.get("provider_type") == "mcp":
        return enrich(_mcp_tools_from_snapshot(
            connector_type_id=connector_type["id"],
            snapshot_json=connector_type.get("tool_snapshot_json"),
            disabled_actions=disabled_actions,
            include_disabled=include_disabled,
            query=query,
            limit=limit,
            offset=offset,
        ))

    if connector_type.get("operations_json"):
        from app.services import openapi_service

        return enrich(openapi_service.generate_tools(
            connector_type_id=connector_type["id"],
            operations_json=connector_type["operations_json"],
            disabled_actions=disabled_actions,
            include_disabled=include_disabled,
            query=query,
            limit=limit,
            offset=offset,
        ))

    backend_json = connector_type.get("backend_json")
    if backend_json:
        import json as _json

        try:
            backend = _json.loads(backend_json)
        except Exception:
            backend = {}
        requests = backend.get("requests", {})
        manifest_actions = {
            a["name"]: a
            for a in connector_type.get("supported_actions") or []
            if isinstance(a, dict)
        }
        if requests:
            disabled_set = {a for a in (disabled_actions or []) if isinstance(a, str)}
            tools = []
            for name, spec in requests.items():
                if query and query.lower() not in name.lower():
                    continue
                if not include_disabled and name in disabled_set:
                    continue
                action_meta = manifest_actions.get(name, {})
                tools.append(
                    {
                        "name": name,
                        "action": name,
                        "method": spec.get("method", ""),
                        "path": spec.get("path", ""),
                        "description": action_meta.get("description", ""),
                        "enabled": name not in disabled_set,
                        "input_schema": action_meta.get("input_schema", {}),
                        "side_effect": action_meta.get(
                            "side_effect", spec.get("side_effect", "none")
                        ),
                        "capability_policy": capability_policy(action_meta),
                    }
                )
            total = len(tools)
            page = tools[offset : offset + limit] if offset or limit != 20 else tools
            return enrich({
                "tools": page,
                "total": total,
                "connector_type_id": connector_type["id"],
            })

    disabled_set = {
        action for action in (disabled_actions or []) if isinstance(action, str)
    }
    supported_action_names = normalize_action_names(
        connector_type.get("supported_actions")
    )
    manifest_actions = {
        item.get("name"): item for item in connector_type.get("supported_actions") or []
        if isinstance(item, dict) and item.get("name")
    }
    tools = [
        {
            "name": action,
            "action": action,
            "method": "",
            "path": "",
            "description": "",
            "enabled": action not in disabled_set,
            "capability_policy": capability_policy(manifest_actions.get(action, {})),
        }
        for action in supported_action_names
    ]
    if query:
        q = query.lower()
        tools = [t for t in tools if q in t["name"].lower() or q in t["action"].lower()]
    if not include_disabled:
        tools = [t for t in tools if t["enabled"]]
    total = len(tools)
    page = tools[offset : offset + limit]
    return enrich({"tools": page, "total": total, "connector_type_id": connector_type["id"]})


def test_binding(binding_id: str) -> dict:
    binding = get_binding_with_credential(binding_id)
    if not binding:
        return {
            "success": False,
            "error": "Binding not found",
            "error_code": "NOT_FOUND",
        }
    if not binding.get("enabled"):
        return {
            "success": False,
            "error": "Binding is disabled",
            "error_code": "DISABLED",
        }

    connector_type = get_connector_type(binding["connector_type_id"])
    if not connector_type:
        return {"success": False, "error": "Connector type not found"}

    cred = binding.get("credential")

    executor = _resolve_executor(connector_type)
    if not executor:
        if connector_type.get("provider_type") == "mcp":
            try:
                tools = discover_mcp_binding_tools(
                    binding, connector_type, cred, timeout_cap_ms=10000
                )
                validate_mcp_binding_contract(
                    binding, connector_type, cred, tools=tools
                )
                update_binding(binding_id, last_tested_at=_utc_now(), last_error=None)
                return {
                    "success": True,
                    "tools_discovered": len(tools),
                    "transport": binding.get("transport_type") or "streamable_http",
                }
            except Exception as e:
                update_binding(binding_id, last_tested_at=_utc_now(), last_error=str(e))
                return {"success": False, "error": str(e), "error_code": "TEST_FAILED"}
        return {
            "success": False,
            "error": "No handler for this connector type",
            "error_code": "NO_HANDLER",
        }

    rate_error = _check_rate_limit(binding)
    if rate_error:
        return {"success": False, "error": rate_error}

    try:
        executor_config = _build_executor_config(binding, connector_type)
        result = executor.test_connection(cred, executor_config)
        if result.get("success"):
            update_binding(binding_id, last_tested_at=_utc_now(), last_error=None)
        else:
            update_binding(
                binding_id, last_tested_at=_utc_now(), last_error=result.get("error")
            )
        return result
    except Exception as e:
        update_binding(binding_id, last_tested_at=_utc_now(), last_error=str(e))
        return {"success": False, "error": str(e)}


def build_capability_summary(
    enforcer,
    *,
    connector_type_id: Optional[str] = None,
    scope: Optional[str] = None,
    enabled_only: bool = True,
) -> dict:
    from app.services import credential_service

    # `enabled_only` is a restriction, not a tri-state filter: False means "also
    # include disabled bindings" (no filter), never "only disabled ones".
    enabled_filter = True if enabled_only else None

    connector_types = list_connector_types()
    if connector_type_id:
        connector_types = [
            ct for ct in connector_types if ct["id"] == connector_type_id
        ]

    visible_bindings = []
    if scope:
        if not enforcer.can_read(scope):
            return {
                "connectors": [],
                "total_connectors": 0,
                "visible_bindings": 0,
                "usable_bindings": 0,
            }
        visible_bindings = list_bindings(scope=scope, enabled=enabled_filter)
    elif getattr(enforcer, "is_admin", False):
        visible_bindings = list_bindings(enabled=enabled_filter)
    else:
        for readable_scope in enforcer.filter_readable_scopes(
            list(enforcer.read_scopes)
        ):
            visible_bindings.extend(
                list_bindings(scope=readable_scope, enabled=enabled_filter)
            )

    if connector_type_id:
        visible_bindings = [
            b for b in visible_bindings if b["connector_type_id"] == connector_type_id
        ]

    bindings_by_type: dict[str, list[dict]] = {}
    for binding in visible_bindings:
        bindings_by_type.setdefault(binding["connector_type_id"], []).append(binding)

    # One pass over the execution log for the whole summary, rather than one
    # query per binding inside the loop below.
    failing_by_binding = failing_actions_by_binding()
    credentials_by_id = credential_service.get_credentials_by_ids(
        [
            binding["credential_id"]
            for binding in visible_bindings
            if binding.get("credential_id")
        ]
    )

    summaries = []
    usable_total = 0
    for connector_type in connector_types:
        try:
            action_result = generate_connector_type_tools(
                connector_type,
                disabled_actions=connector_type.get("disabled_actions") or [],
                include_disabled=False,
                limit=1,
            )
            action_count = int(action_result.get("total", 0) or 0)
            action_discovery = {
                "success": True,
                "action_count": action_count,
            }
        except Exception as exc:
            action_count = 0
            action_discovery = {
                "success": False,
                "action_count": 0,
                "error": str(exc),
            }

        binding_summaries = []
        scopes = set()
        for binding in bindings_by_type.get(connector_type["id"], []):
            scopes.add(binding["scope"])
            credential_present = False
            credential_readable = False
            credential_scope = None
            if binding.get("credential_id"):
                credential = credentials_by_id.get(binding["credential_id"])
                if credential:
                    credential_present = True
                    credential_scope = credential.get("scope")
                    credential_readable = enforcer.can_read(credential_scope)

            binding_config = _parse_json_object(binding.get("config_json")) or {}
            auth_required = _connector_requires_credential(connector_type, binding_config)
            credential_ready = (not auth_required) or credential_present
            usable = (
                bool(binding.get("enabled"))
                and credential_ready
                and action_discovery["success"]
                and action_count > 0
            )
            if usable:
                usable_total += 1

            if not binding.get("last_tested_at"):
                test_status = "unknown"
            elif binding.get("last_error"):
                test_status = "failed"
            else:
                test_status = "passed"

            # A binding can pass its health check while one of its actions fails
            # steadily; the probe only proves the connection works.
            failing_actions = failing_by_binding.get(binding["id"], [])

            binding_summaries.append(
                {
                    "id": binding["id"],
                    "name": binding["name"],
                    "scope": binding["scope"],
                    "enabled": bool(binding.get("enabled")),
                    "credential": {
                        "present": credential_present,
                        "readable": credential_readable,
                        "scope": credential_scope,
                    },
                    "health": {
                        "test_status": test_status,
                        "last_tested_at": binding.get("last_tested_at"),
                        "last_error": binding.get("last_error"),
                        "failing_actions": failing_actions,
                    },
                    "usable_by_caller": usable,
                }
            )

        summaries.append(
            {
                "id": connector_type["id"],
                "display_name": connector_type["display_name"],
                "provider_type": connector_type.get("provider_type") or "openapi",
                "auth_type": connector_type.get("auth_type"),
                "action_count": action_count,
                "action_discovery": action_discovery,
                "binding_count": len(binding_summaries),
                "visible_scopes": sorted(scopes),
                "bindings": binding_summaries,
            }
        )

    return {
        "connectors": summaries,
        "total_connectors": len(summaries),
        "visible_bindings": sum(len(c["bindings"]) for c in summaries),
        "usable_bindings": usable_total,
    }


def _validate_action_for_connector(connector_type: dict, action: str) -> Optional[str]:
    disabled_actions = set(connector_type.get("disabled_actions") or [])
    if action in disabled_actions:
        return "DISABLED_ACTION"

    if connector_type.get("backend_type") == "generic_http":
        return None

    if connector_type.get("provider_type") == "mcp":
        snapshot = connector_type.get("tool_snapshot_json")
        if snapshot:
            try:
                meta = json.loads(snapshot)
                valid_actions = {
                    tool.get("name")
                    for tool in meta.get("tools", [])
                    if isinstance(tool, dict) and tool.get("name")
                }
                if valid_actions and action not in valid_actions:
                    return "INVALID_ACTION"
            except json.JSONDecodeError:
                pass
        elif action not in normalize_action_names(
            connector_type.get("supported_actions")
        ):
            return "INVALID_ACTION"
        return None

    supported_actions = set(
        normalize_action_names(connector_type.get("supported_actions"))
    )

    ops_meta = None
    if connector_type.get("operations_json"):
        try:
            ops_meta = json.loads(connector_type["operations_json"])
        except json.JSONDecodeError:
            pass

    if ops_meta:
        valid_actions = {op["operation_id"] for op in ops_meta.get("operations", [])}
        if action not in valid_actions:
            return "INVALID_ACTION"
    elif action not in supported_actions:
        return "INVALID_ACTION"
    return None


def _validate_action_params(
    connector_type: dict, action: str, params: Optional[dict]
) -> Optional[dict]:
    """Validate caller params against the action's declared input_schema before
    executing. Returns an error dict to abort, or None to proceed.

    This is a safety gate: it stops malformed, missing, or empty required params
    from ever reaching the connector. It matters most for destructive actions
    whose backend treats an omitted/empty selector as "apply to ALL" (e.g.
    Transmission torrent-remove with no ids removes the entire queue). Declaring
    the selector required with minItems>=1 turns "remove all by accident" into a
    rejected call.
    """
    meta = None
    for a in connector_type.get("supported_actions") or []:
        if isinstance(a, dict) and a.get("name") == action:
            meta = a
            break
    schema = (meta or {}).get("input_schema")
    if not isinstance(schema, dict) or not schema.get("properties"):
        return None  # nothing declared to validate against
    try:
        import jsonschema

        jsonschema.validate(instance=params or {}, schema=schema)
    except jsonschema.ValidationError as e:
        return {
            "success": False,
            "error": f"Invalid parameters for '{action}': {e.message}",
            "error_code": "INVALID_PARAMS",
        }
    except Exception:
        # A real ValidationError above always blocks. Only swallow validator
        # infrastructure errors (e.g. a malformed schema) so they don't wedge
        # every call, but log them loudly.
        logger.exception("param schema validation failed to run for action %s", action)
    return None


def _action_meta(connector_type: dict, action: str) -> dict:
    for a in connector_type.get("supported_actions") or []:
        if isinstance(a, dict) and a.get("name") == action:
            return a
    try:
        operations = json.loads(connector_type.get("operations_json") or "{}").get(
            "operations", []
        )
    except json.JSONDecodeError:
        operations = []
    for operation in operations:
        if isinstance(operation, dict) and operation.get("operation_id") == action:
            return {
                "name": action,
                "method": operation.get("method"),
                "description": operation.get("description") or operation.get("summary"),
            }
    return {}


def _prepare_action_params(
    connector_type: dict,
    action: str,
    params: Optional[dict],
    binding_config: Optional[dict],
) -> dict:
    caller_params = params if isinstance(params, dict) else {}
    normalized = dict(caller_params)
    meta = _action_meta(connector_type, action)
    aliases = meta.get("param_aliases") or {}
    if isinstance(aliases, dict):
        for alias, canonical in aliases.items():
            if (
                isinstance(alias, str)
                and isinstance(canonical, str)
                and alias in caller_params
                and canonical not in caller_params
            ):
                normalized[canonical] = caller_params[alias]

    schema = meta.get("input_schema") or {}
    properties = schema.get("properties") if isinstance(schema, dict) else None
    if isinstance(properties, dict):
        for name, value in caller_params.items():
            if not isinstance(name, str):
                continue
            canonical = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
            if (
                canonical != name
                and canonical in properties
                and canonical not in caller_params
            ):
                normalized[canonical] = value

    defaults = {}
    if isinstance(binding_config, dict):
        configured_defaults = binding_config.get("default_params")
        if isinstance(configured_defaults, dict):
            defaults = configured_defaults
    return {**defaults, **normalized}


def execute_binding_action(
    binding_id: str, action: str, params: Optional[dict] = None
) -> dict:
    binding = get_binding(binding_id)
    if not binding:
        return {
            "success": False,
            "error": "Binding not found",
            "error_code": "NOT_FOUND",
        }
    if not binding.get("enabled"):
        return {
            "success": False,
            "error": "Binding is disabled",
            "error_code": "DISABLED",
        }

    connector_type = get_connector_type(binding["connector_type_id"])
    if not connector_type:
        return {"success": False, "error": "Connector type not found"}

    action_error = _validate_action_for_connector(connector_type, action)
    if action_error:
        error_messages = {
            "DISABLED_ACTION": f"Action disabled: {action}",
            "INVALID_ACTION": f"Action not supported: {action}",
        }
        return {
            "success": False,
            "error": error_messages.get(action_error, "Action validation failed"),
            "error_code": action_error,
        }
    if params is not None and not isinstance(params, dict):
        return {
            "success": False,
            "error": f"Invalid parameters for '{action}': params must be an object",
            "error_code": "INVALID_PARAMS",
        }

    binding_config = {}
    if binding.get("config_json"):
        try:
            binding_config = json.loads(binding["config_json"])
        except json.JSONDecodeError:
            pass

    effective_params = _prepare_action_params(
        connector_type, action, params, binding_config
    )
    param_error = _validate_action_params(connector_type, action, effective_params)
    if param_error:
        return param_error

    binding_with_cred = get_binding_with_credential(binding_id)
    cred = binding_with_cred.get("credential")
    # `cred is None`, not `not cred`: Credential.__bool__ is keyed off `.raw`,
    # which a resolved multi-field (basic/oauth2) credential can legitimately
    # have falsy — that must still count as "a credential is linked".
    if _connector_requires_credential(connector_type, binding_config) and cred is None:
        return {
            "success": False,
            "error": "No credential linked to this binding",
            "error_code": "NO_CREDENTIAL",
        }

    rate_error = _check_rate_limit(binding)
    if rate_error:
        return {
            "success": False,
            "error": rate_error,
            "error_code": "RATE_LIMITED",
        }

    max_retries = 0
    retry_base_delay = 1.0
    if binding.get("rate_limit_config_json"):
        try:
            rc = json.loads(binding["rate_limit_config_json"])
            max_retries = max(0, int(rc.get("max_retries", 0)))
            retry_base_delay = max(0.1, int(rc.get("retry_delay_ms", 1000)) / 1000.0)
        except (json.JSONDecodeError, ValueError, TypeError):
            pass

    provider_type = connector_type.get("provider_type") or "openapi"
    executor = _resolve_executor(connector_type)
    uses_session = bool(executor and getattr(executor, "needs_session", False))

    def _run_once() -> dict:
        nonlocal cred
        try:
            if provider_type == "mcp":
                from app.services import mcp_provider_service as _mcp

                endpoint_url = effective_mcp_endpoint(binding, connector_type)
                if not endpoint_url:
                    return {
                        "success": False,
                        "error": "MCP connector has no endpoint_url",
                        "error_code": "INVALID_CONFIGURATION",
                    }
                # Validate immediately before passing the selected binding/type
                # endpoint to the provider. The provider intentionally also
                # serves lower-level direct callers, so this boundary is where
                # binding override URL policy must be enforced.
                endpoint_url = _mcp.validate_mcp_server_url(endpoint_url)
                result = _mcp.execute_mcp_tool(
                    endpoint_url=endpoint_url,
                    action=action,
                    params=effective_params,
                    credential=cred.raw if cred else None,
                    config_json=binding.get("config_json"),
                    transport_type=connector_type.get("transport_type")
                    or "streamable_http",
                )
                return {
                    "success": result.success,
                    "body": result.body,
                    "error": result.error,
                    "error_code": result.error_code,
                    "status": result.status,
                    "transport": result.transport,
                }

            if not executor:
                return {
                    "success": False,
                    "error": "Connector handler not found",
                    "error_code": "NOT_FOUND",
                }

            def _do_execute(session):
                executor_config = _build_executor_config(binding, connector_type)
                return executor.execute(
                    action=action,
                    params=effective_params,
                    credential=cred,
                    config_json=executor_config,
                    session=session,
                )

            if not uses_session:
                return _do_execute(None)

            from app.services import connector_session_service as _sessions
            from app.connectors.errors import SessionExpiredError, AuthExpiredError

            session = _sessions.load_session(binding_id)
            try:
                return _do_execute(session)
            except (SessionExpiredError, AuthExpiredError):
                with _sessions.binding_lock(binding_id):
                    session = _sessions.load_session(binding_id)
                    try:
                        return _do_execute(session)
                    except (SessionExpiredError, AuthExpiredError):
                        refreshed = executor.refresh_session(
                            cred,
                            binding_config,
                            session,
                        )
                        _sessions.save_session(
                            binding_id,
                            refreshed.get("session"),
                            refreshed.get("expires_at"),
                        )
                        upd = refreshed.get("credential_update")
                        if upd and cred and cred.reference_name:
                            from app.services import credential_service as _cs
                            import json

                            new_blob = json.dumps({**cred.fields, **upd})
                            _cs.update_credential_value(cred.reference_name, new_blob)
                            cred = _cs.resolve_credential(cred.reference_name)
                        return _do_execute(refreshed.get("session"))
        except Exception as e:
            return {
                "success": False,
                "error": str(e),
                "error_code": "EXECUTION_ERROR",
            }

    result = _run_once()
    for attempt in range(1, max_retries + 1):
        if not _is_transient_result(result):
            break
        delay = min(
            retry_base_delay * (2 ** (attempt - 1)) + random.uniform(0, 0.5), 30.0
        )
        logger.info(
            "Connector retry %d/%d for binding %s after transient failure (delay %.2fs): %s",
            attempt,
            max_retries,
            binding_id,
            delay,
            result.get("error"),
        )
        time.sleep(delay)
        result = _run_once()

    return result


def execute_authorized_binding_action_with_logging(
    binding_id: str, action: str, params: Optional[dict], authority
) -> dict:
    """Protected connector entry point; authority is never optional."""
    if authority is None:
        raise TypeError("An explicit effective or system authority is required")
    from app.security.effective_authority import SystemAuthority

    if isinstance(authority, SystemAuthority):
        token = _execution_authority.set(authority)
        try:
            return execute_binding_action_with_logging(binding_id, action, params)
        finally:
            _execution_authority.reset(token)
    binding_for_auth = get_binding(binding_id)
    connector_type = get_connector_type(binding_for_auth["connector_type_id"]) if binding_for_auth else None
    if not binding_for_auth or not connector_type or not can_run_binding_action(
        authority, binding_for_auth, connector_type, action
    ):
        from app.security.exceptions import APIError

        raise APIError("SCOPE_DENIED", "Access denied to this binding action", 403)
    token = _execution_authority.set(authority)
    try:
        return execute_binding_action_with_logging(binding_id, action, params)
    finally:
        _execution_authority.reset(token)


def audit_delegated_execution_denial(authority, binding_id: str, action: str) -> None:
    if not getattr(authority, "is_delegated", False):
        return
    from app.services import audit_service

    details = authority.safe_attribution()
    details["action"] = action
    audit_service.write_event(
        actor_type=authority.actor_type, actor_id=authority.actor_id,
        action="connector_action_executed", resource_type="connector_binding",
        resource_id=binding_id, result="blocked", details=details,
    )


def can_run_binding_action(authority, binding: dict, connector_type: dict, action: str) -> bool:
    return authority.can_binding_action(binding["id"], action, scope=binding["scope"])


def execute_binding_action_with_logging(
    binding_id: str, action: str, params: Optional[dict] = None
) -> dict:
    """Execution/logging primitive. Request paths must use the authorized wrapper."""
    from app.services.event_stream_service import event_hub
    from app.services import webhook_service

    start = time.time()
    result = execute_binding_action(binding_id, action, params)
    duration_ms = int((time.time() - start) * 1000)
    result = dict(result)
    result["duration_ms"] = duration_ms
    log_execution(
        binding_id=binding_id,
        action=action,
        params_json=json.dumps(params or {}),
        result_status="success" if result.get("success") else "failure",
        result_body_json=json.dumps(result) if result.get("success") else None,
        error_message=result.get("error") if not result.get("success") else None,
        duration_ms=duration_ms,
        error_code=result.get("error_code") if not result.get("success") else None,
        failure_category=(
            classify_failure(result.get("error_code"), result.get("error"))
            if not result.get("success") else None
        ),
    )
    binding = get_binding(binding_id) or {}
    connector_type = (
        get_connector_type(binding.get("connector_type_id", ""))
        if binding.get("connector_type_id")
        else None
    )
    _event_data = {
        "binding_id": binding_id,
        "binding_name": binding.get("name"),
        "scope": binding.get("scope"),
        "connector_type_id": binding.get("connector_type_id"),
        "connector_type_name": connector_type.get("display_name")
        if connector_type
        else None,
        "action": action,
        "success": result.get("success"),
        "duration_ms": duration_ms,
        "status": result.get("error_code")
        or ("success" if result.get("success") else "failure"),
        "error_message": result.get("error") if not result.get("success") else None,
    }
    event_hub.publish("connector_executed", _event_data)
    webhook_service.dispatch_event("connector_executed", _event_data)
    return result


# An execution log is a record that a call happened and how it went. It is not
# a cache of what the call returned. Storing whole response bodies made the log
# 142 MB of a 180 MB database on the first real deployment — 1,463 rows at an
# average of 101 KB, one of them 3.5 MB — for a table nothing reads in full.
# Truncation keeps the head, which is where the shape of a response and any
# error detail live.
EXECUTION_BODY_MAX_CHARS = 16_000

# Failure rate at which an action is worth reporting, with a minimum call count
# so one bad call out of two does not read as a broken connector.
FAILING_ACTION_THRESHOLD = 0.2
FAILING_ACTION_MIN_CALLS = 5

FAILURE_CATEGORIES = frozenset({
    "caller_validation", "credential", "remote_service", "network", "rate_limited", "unknown",
})


def classify_failure(error_code: Optional[str], error_message: Optional[str]) -> str:
    """Classify a connector failure without exposing request or credential data."""
    code = (error_code or "").upper()
    message = (error_message or "").lower()
    if code in {"INVALID_ACTION", "INVALID_REQUEST", "INVALID_CONFIGURATION", "DISABLED_ACTION"}:
        return "caller_validation"
    if "invalid parameters" in message or "is not of type" in message or "required property" in message:
        return "caller_validation"
    if code in {"NO_CREDENTIAL", "AUTH_EXPIRED", "UNAUTHORIZED", "FORBIDDEN"}:
        return "credential"
    if code == "RATE_LIMITED" or "rate limit" in message:
        return "rate_limited"
    if "timeout" in message or "connection" in message or "network" in message:
        return "network"
    if code in {"EXECUTION_ERROR", "PROVIDER_ERROR"}:
        return "remote_service"
    return "unknown"


def _truncate_execution_body(body: Optional[str]) -> Optional[str]:
    if body is None or len(body) <= EXECUTION_BODY_MAX_CHARS:
        return body
    dropped = len(body) - EXECUTION_BODY_MAX_CHARS
    return (
        body[:EXECUTION_BODY_MAX_CHARS]
        + f"\n…[execution log truncated: {dropped:,} more characters]"
    )


def log_execution(
    binding_id: str,
    action: str,
    params_json: Optional[str],
    result_status: str,
    result_body_json: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
    error_code: Optional[str] = None,
    failure_category: Optional[str] = None,
) -> str:
    execution_id = secrets.token_urlsafe(16)
    result_body_json = _truncate_execution_body(result_body_json)
    authority = _execution_authority.get()
    context = getattr(authority, "context", None)
    attribution = (
        getattr(authority, "actor_type", None),
        getattr(authority, "actor_id", None),
        getattr(authority, "principal_user_id", None),
        getattr(context, "agent_id", None),
        getattr(authority, "issuer_actor_id", None),
        getattr(authority, "coordinator_agent_id", None),
        getattr(authority, "grant_id", None),
        getattr(authority, "correlation_id", None),
        "delegated" if getattr(authority, "grant_id", None) else "system" if authority and not context else "permanent" if authority else None,
    )
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_executions
            (id, binding_id, action, params_json, result_status, result_body_json, error_message, duration_ms,
             actor_type, actor_id, principal_user_id, executor_agent_id, issuer_actor_id,
             coordinator_agent_id, grant_id, correlation_id, authorization_mode, error_code, failure_category)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                execution_id,
                binding_id,
                action,
                params_json,
                result_status,
                result_body_json,
                error_message,
                duration_ms,
                *attribution,
                error_code,
                failure_category,
            ),
        )
        conn.commit()
    return execution_id


def failing_actions_by_binding(days: int = 30) -> dict[str, list[dict]]:
    """Failing actions for every binding in one query.

    The per-binding call is fine on its own, but the connectors page and the
    capability summary both loop over bindings, which turned one report into one
    query per row. Grouping once and indexing by binding keeps those O(1).
    """
    grouped: dict[str, list[dict]] = {}
    for stat in action_health(days=days):
        if stat["failure_rate"] >= FAILING_ACTION_THRESHOLD and stat["calls"] >= FAILING_ACTION_MIN_CALLS:
            grouped.setdefault(stat["binding_id"], []).append(stat)
    return grouped


def action_health(binding_id: Optional[str] = None, days: int = 30) -> list[dict]:
    """Per-action success rates from the execution log.

    Surfaces what per-binding health checks cannot: a binding that connects
    fine while one of its actions fails half the time. On the first real
    deployment one binding's list action had been failing 81 times in 216 calls
    for two months with nothing reporting it.
    """
    conditions = ["executed_at >= datetime('now', ?)"]
    params: list = [f"-{max(days, 1)} days"]
    if binding_id:
        conditions.append("ce.binding_id = ?")
        params.append(binding_id)

    with get_db() as conn:
        rows = conn.execute(
            f"""
            SELECT ce.binding_id, cb.name AS binding_name, ce.action,
                   COUNT(*) AS calls,
                   SUM(CASE WHEN ce.result_status != 'success' THEN 1 ELSE 0 END) AS failures,
                   GROUP_CONCAT(DISTINCT CASE WHEN ce.result_status != 'success' THEN ce.failure_category END) AS failure_categories,
                   MAX(ce.executed_at) AS last_called_at
            FROM connector_executions ce
            LEFT JOIN connector_bindings cb ON cb.id = ce.binding_id
            WHERE {' AND '.join(conditions)}
            GROUP BY ce.binding_id, ce.action
            ORDER BY failures DESC, calls DESC
            """,
            params,
        ).fetchall()

    health = []
    for row in rows:
        calls = int(row["calls"] or 0)
        failures = int(row["failures"] or 0)
        health.append(
            {
                "binding_id": row["binding_id"],
                "binding_name": row["binding_name"],
                "action": row["action"],
                "calls": calls,
                "failures": failures,
                "failure_rate": round(failures / calls, 3) if calls else 0.0,
                "failure_categories": sorted(filter(None, (row["failure_categories"] or "").split(","))),
                "last_called_at": row["last_called_at"],
            }
        )
    return health


def prune_executions(retention_days: int) -> int:
    """Delete execution log rows older than the retention window."""
    if retention_days <= 0:
        return 0
    with get_db() as conn:
        cursor = conn.execute(
            "DELETE FROM connector_executions WHERE executed_at < datetime('now', ?)",
            (f"-{retention_days} days",),
        )
        conn.commit()
        return cursor.rowcount or 0


def list_executions(binding_id: str, limit: int = 50, offset: int = 0) -> list[dict]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM connector_executions WHERE binding_id = ? ORDER BY executed_at DESC LIMIT ? OFFSET ?",
            (binding_id, limit, offset),
        ).fetchall()
        return [dict(row) for row in rows]


def _normalize_scope(scope: str) -> str:
    parts = scope.split(":", 1)
    if len(parts) == 2 and parts[0].lower() in ("user", "agent", "workspace", "shared"):
        return f"{parts[0].lower()}:{normalize_id(parts[1])}"
    return scope


def create_connector_type(
    connector_type_id: str,
    display_name: str,
    description: Optional[str] = None,
    provider_type: str = "openapi",
    auth_type: str = "bearer",
    supported_actions: Optional[list[str]] = None,
    required_credential_fields: Optional[list[str]] = None,
    disabled_actions: Optional[list[str]] = None,
    capability_policy_overrides_json: Optional[str] = None,
    endpoint_url: Optional[str] = None,
    transport_type: Optional[str] = None,
    capabilities_json: Optional[str] = None,
    tool_snapshot_json: Optional[str] = None,
    spec_url: Optional[str] = None,
    operations_json: Optional[str] = None,
    backend_type: Optional[str] = None,
    backend_json: Optional[str] = None,
) -> dict:
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_types
            (id, display_name, description, provider_type, auth_type,
             supported_actions_json, required_credential_fields_json,
             disabled_actions_json, capability_policy_overrides_json, endpoint_url, transport_type,
             capabilities_json, tool_snapshot_json, spec_url, operations_json,
             backend_type, backend_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                connector_type_id,
                display_name,
                description,
                provider_type,
                auth_type,
                json.dumps(supported_actions or []),
                json.dumps(required_credential_fields or []),
                json.dumps(disabled_actions or []),
                capability_policy_overrides_json or "{}",
                endpoint_url,
                transport_type,
                capabilities_json,
                tool_snapshot_json,
                spec_url,
                operations_json,
                backend_type,
                backend_json,
            ),
        )
        conn.commit()
    return get_connector_type(connector_type_id)


def update_connector_type(connector_type_id: str, **fields) -> bool:
    allowed = (
        "display_name",
        "description",
        "provider_type",
        "auth_type",
        "supported_actions_json",
        "required_credential_fields_json",
        "disabled_actions_json",
        "capability_policy_overrides_json",
        "endpoint_url",
        "transport_type",
        "capabilities_json",
        "tool_snapshot_json",
        "spec_url",
        "operations_json",
        "backend_type",
        "backend_json",
        "is_active",
    )
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed:
            if key == "is_active":
                updates.append("is_active = ?")
                params.append(1 if val else 0)
            else:
                updates.append(f"{key} = ?")
                params.append(val)
    if not updates:
        return False
    updates.append("updated_at = CURRENT_TIMESTAMP")
    params.append(connector_type_id)
    with get_db() as conn:
        cursor = conn.execute(
            f"UPDATE connector_types SET {', '.join(updates)} WHERE id = ?",
            params,
        )
        conn.commit()
        return cursor.rowcount > 0


def delete_connector_type(connector_type_id: str) -> bool:
    with get_db() as conn:
        bindings = conn.execute(
            "SELECT id FROM connector_bindings WHERE connector_type_id = ?",
            (connector_type_id,),
        ).fetchall()
        for row in bindings:
            conn.execute(
                "DELETE FROM connector_executions WHERE binding_id = ?",
                (row[0],),
            )
        conn.execute(
            """UPDATE delegation_requests SET grant_id = NULL WHERE grant_id IN
               (SELECT dga.grant_id FROM delegated_grant_actions dga
                JOIN connector_bindings cb ON cb.id = dga.binding_id WHERE cb.connector_type_id = ?)""",
            (connector_type_id,),
        )
        conn.execute(
            "DELETE FROM delegated_grants WHERE id IN (SELECT dga.grant_id FROM delegated_grant_actions dga JOIN connector_bindings cb ON cb.id = dga.binding_id WHERE cb.connector_type_id = ?)",
            (connector_type_id,),
        )
        conn.execute(
            "DELETE FROM connector_bindings WHERE connector_type_id = ?",
            (connector_type_id,),
        )
        cursor = conn.execute(
            "DELETE FROM connector_types WHERE id = ?",
            (connector_type_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def update_connector_type_actions(
    connector_type_id: str, disabled_actions: list[str]
) -> bool:
    normalized = _normalize_action_list(disabled_actions)
    with get_db() as conn:
        cursor = conn.execute(
            """
            UPDATE connector_types
            SET disabled_actions_json = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (json.dumps(normalized), connector_type_id),
        )
        conn.commit()
        return cursor.rowcount > 0


def _normalize_action_list(actions: list[str]) -> list[str]:
    seen = set()
    normalized = []
    for action in actions or []:
        if not isinstance(action, str):
            continue
        cleaned = action.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            normalized.append(cleaned)
    return normalized


def _utc_now() -> str:
    from app.time_utils import utc_now_iso

    return utc_now_iso()
