import json
import logging
import random
import secrets
import time
from typing import Optional
from app.database import get_db
from app.models.enums import normalize_id
from app.services import mcp_provider_service

logger = logging.getLogger(__name__)


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


def list_connector_types(include_inactive: bool = False) -> list[dict]:
    from app.services import adapter_loader

    with get_db() as conn:
        query = """
            SELECT id, display_name, description, provider_type, auth_type,
                   supported_actions_json, required_credential_fields_json,
                   default_binding_rules_json, disabled_actions_json, endpoint_url,
                   transport_type, capabilities_json, tool_snapshot_json, spec_url,
                   operations_json, backend_type, backend_json,
                   is_active, created_at, updated_at
            FROM connector_types
        """
        if not include_inactive:
            query += " WHERE is_active = 1"
        query += " ORDER BY display_name"
        rows = conn.execute(query).fetchall()
        connector_types = []
        for row in rows:
            ct = _row_to_connector_type(dict(row))
            adapter_entry = adapter_loader.get_adapter_library_entry(ct["id"])
            if adapter_entry and not adapter_entry.get("installed"):
                continue
            connector_types.append(ct)
        return connector_types


def get_connector_type(connector_type_id: str) -> Optional[dict]:
    with get_db() as conn:
        row = conn.execute(
            """
            SELECT id, display_name, description, provider_type, auth_type,
                   supported_actions_json, required_credential_fields_json,
                   default_binding_rules_json, disabled_actions_json, endpoint_url,
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
) -> dict:
    normalized_scope = _normalize_scope(scope)
    config_data = _parse_json_object(config_json)
    if config_json is not None and config_data is None:
        raise ValueError("config_json must be a JSON object")
    config_json = json.dumps(config_data) if config_data is not None else None
    binding_id = secrets.token_urlsafe(16)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_bindings
            (id, connector_type_id, name, scope, credential_id, config_json, enabled, created_by)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
    )
    # Status fields are nullable and must be clearable: a successful test has to
    # reset last_error to NULL, otherwise a binding that failed once would keep
    # reporting an error forever. Other fields keep the "skip None" semantics.
    nullable = ("last_tested_at", "last_error")
    updates = []
    params = []
    for key, val in fields.items():
        if key in allowed and (val is not None or key in nullable):
            if key == "enabled":
                updates.append("enabled = ?")
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


def delete_binding(binding_id: str) -> bool:
    with get_db() as conn:
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


def _infer_backend_type(connector_type: dict) -> Optional[str]:
    """Infer backend type from legacy fields for existing connector_types rows."""
    if connector_type.get("provider_type") == "mcp":
        return "mcp"
    if connector_type.get("operations_json"):
        return "openapi"
    if connector_type.get("id") == "generic_http":
        return "generic_http"
    return None


def _resolve_executor(connector_type: dict):
    from app.connectors import get_connector

    registered = get_connector(connector_type["id"])
    if registered:
        return registered

    backend = connector_type.get("backend_type") or _infer_backend_type(connector_type)

    if backend == "generic_http" or connector_type.get("provider_type") == "generic_http":
        from app.connectors.generic_http import GenericHttpConnector

        return GenericHttpConnector()

    if backend == "http":
        from app.connectors.http_engine import HttpEngine

        return HttpEngine(connector_type)

    if backend == "openapi" or connector_type.get("operations_json"):
        from app.connectors.openapi_executor import OpenApiExecutor

        return OpenApiExecutor()

    if backend == "cli":
        from app.connectors.cli_engine import CliEngine

        return CliEngine(connector_type)

    return None


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
    if connector_type.get("provider_type") == "mcp":
        return _mcp_tools_from_snapshot(
            connector_type_id=connector_type["id"],
            snapshot_json=connector_type.get("tool_snapshot_json"),
            disabled_actions=disabled_actions,
            include_disabled=include_disabled,
            query=query,
            limit=limit,
            offset=offset,
        )

    if connector_type.get("operations_json"):
        from app.services import openapi_service

        return openapi_service.generate_tools(
            connector_type_id=connector_type["id"],
            operations_json=connector_type["operations_json"],
            disabled_actions=disabled_actions,
            include_disabled=include_disabled,
            query=query,
            limit=limit,
            offset=offset,
        )

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
                    }
                )
            total = len(tools)
            page = tools[offset : offset + limit] if offset or limit != 20 else tools
            return {
                "tools": page,
                "total": total,
                "connector_type_id": connector_type["id"],
            }

    disabled_set = {
        action for action in (disabled_actions or []) if isinstance(action, str)
    }
    supported_action_names = normalize_action_names(
        connector_type.get("supported_actions")
    )
    tools = [
        {
            "name": action,
            "action": action,
            "method": "",
            "path": "",
            "description": "",
            "enabled": action not in disabled_set,
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
    return {"tools": page, "total": total, "connector_type_id": connector_type["id"]}


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
                test_binding_context = dict(binding)
                test_binding_context["endpoint_url"] = connector_type.get(
                    "endpoint_url"
                )
                endpoint_url, headers, timeout_ms = (
                    mcp_provider_service.build_mcp_request_config(
                        test_binding_context, credential=cred.raw if cred else None
                    )
                )
                tools = mcp_provider_service.discover_all_tools(
                    endpoint_url,
                    timeout_ms=min(timeout_ms, 10000),
                    headers=headers,
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
        visible_bindings = list_bindings(scope=scope, enabled=enabled_only)
    elif getattr(enforcer, "is_admin", False):
        visible_bindings = list_bindings(enabled=enabled_only)
    else:
        for readable_scope in enforcer.filter_readable_scopes(
            list(enforcer.read_scopes)
        ):
            visible_bindings.extend(
                list_bindings(scope=readable_scope, enabled=enabled_only)
            )

    if connector_type_id:
        visible_bindings = [
            b for b in visible_bindings if b["connector_type_id"] == connector_type_id
        ]

    bindings_by_type: dict[str, list[dict]] = {}
    for binding in visible_bindings:
        bindings_by_type.setdefault(binding["connector_type_id"], []).append(binding)

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
                credential = credential_service.get_credential(binding["credential_id"])
                if credential:
                    credential_present = True
                    credential_scope = credential.get("scope")
                    credential_readable = enforcer.can_read(credential_scope)

            binding_config = _parse_json_object(binding.get("config_json")) or {}
            auth_overridden = binding_config.get("auth_mode") == "none"
            auth_required = (
                connector_type.get("auth_type") != "none" and not auth_overridden
            )
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

    if connector_type.get("provider_type") == "generic_http":
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

    param_error = _validate_action_params(connector_type, action, params)
    if param_error:
        return param_error

    binding_with_cred = get_binding_with_credential(binding_id)
    cred = binding_with_cred.get("credential")
    binding_config = {}
    if binding.get("config_json"):
        try:
            binding_config = json.loads(binding["config_json"])
        except json.JSONDecodeError:
            pass
    auth_overridden = binding_config.get("auth_mode") == "none"
    if connector_type.get("auth_type") != "none" and not cred and not auth_overridden:
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

                endpoint_url = connector_type.get("endpoint_url")
                if not endpoint_url:
                    return {
                        "success": False,
                        "error": "MCP connector has no endpoint_url",
                        "error_code": "INVALID_CONFIGURATION",
                    }
                result = _mcp.execute_mcp_tool(
                    endpoint_url=endpoint_url,
                    action=action,
                    params=params or {},
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
                    params=params or {},
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


def execute_binding_action_with_logging(
    binding_id: str, action: str, params: Optional[dict] = None
) -> dict:
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


def log_execution(
    binding_id: str,
    action: str,
    params_json: Optional[str],
    result_status: str,
    result_body_json: Optional[str] = None,
    error_message: Optional[str] = None,
    duration_ms: Optional[int] = None,
) -> str:
    execution_id = secrets.token_urlsafe(16)
    with get_db() as conn:
        conn.execute(
            """
            INSERT INTO connector_executions
            (id, binding_id, action, params_json, result_status, result_body_json, error_message, duration_ms)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
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
            ),
        )
        conn.commit()
    return execution_id


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
             disabled_actions_json, endpoint_url, transport_type,
             capabilities_json, tool_snapshot_json, spec_url, operations_json,
             backend_type, backend_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
