import json
import logging
import urllib.parse
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import urlparse

import httpx

from app.security.url_validation import validate_public_url


DEFAULT_MCP_TIMEOUT_MS = 60000
DEFAULT_MCP_PROTOCOL_VERSION = "2025-03-26"
_MCP_ACCEPT = "application/json, text/event-stream"
logger = logging.getLogger(__name__)


@dataclass
class MCPDiscoveryResult:
    server_name: str
    protocol_version: str
    capabilities: dict[str, Any]
    tools: list[dict[str, Any]]


@dataclass
class MCPExecutionResult:
    success: bool
    body: Any = None
    error: Optional[str] = None
    error_code: Optional[str] = None
    status: Optional[int] = None
    transport: str = "streamable_http"


def validate_mcp_server_url(url: str) -> str:
    validate_public_url(url)
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("MCP server URL must use http or https")
    if not parsed.netloc:
        raise ValueError("MCP server URL must include a host")
    return url.rstrip("/")


def _parse_mcp_response(response: httpx.Response) -> dict[str, Any]:
    """Parse a JSON-RPC response that may be plain JSON or SSE (text/event-stream)."""
    content_type = response.headers.get("content-type", "")
    if "text/event-stream" in content_type:
        # Extract the last JSON object from SSE data lines
        data: dict[str, Any] = {}
        for line in response.text.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw and raw != "[DONE]":
                    try:
                        data = json.loads(raw)
                    except json.JSONDecodeError:
                        pass
        return data
    return response.json()


def _jsonrpc_request(
    client: httpx.Client,
    url: str,
    method: str,
    params: Optional[dict[str, Any]] = None,
    request_id: int = 1,
) -> dict[str, Any]:
    payload = {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": params or {},
    }
    response = client.post(url, json=payload)
    response.raise_for_status()
    # Streamable-HTTP servers issue a session id on initialize that must be sent
    # on every subsequent request. Capture it onto the shared client so following
    # calls (tools/list, tools/call) include it.
    session_id = response.headers.get("mcp-session-id")
    if session_id:
        client.headers["Mcp-Session-Id"] = session_id
    data = _parse_mcp_response(response)
    if isinstance(data, dict) and data.get("error"):
        raise ValueError(data["error"].get("message") or "MCP request failed")
    if not isinstance(data, dict) or "result" not in data:
        raise ValueError("Invalid MCP response")
    return data["result"]


def _mcp_initialize(client: httpx.Client, url: str) -> dict[str, Any]:
    """Perform the MCP initialize handshake on a shared client.

    Captures the session id (via _jsonrpc_request) and sends the required
    notifications/initialized. Tolerates servers that don't implement initialize
    so cold tools/list still works for them.
    """
    try:
        init_result = _jsonrpc_request(
            client,
            url,
            "initialize",
            {
                "protocolVersion": DEFAULT_MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "agent-core", "version": "1.0.0"},
            },
            request_id=1,
        )
    except Exception:
        logger.warning("MCP initialize failed for %s; continuing without a session", url)
        return {}
    try:
        client.post(url, json={"jsonrpc": "2.0", "method": "notifications/initialized"})
    except Exception:
        pass
    return init_result if isinstance(init_result, dict) else {}


def _parse_json_object(value: Optional[str]) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _apply_auth(url: str, headers: dict[str, str], credential: Optional[str], config: dict[str, Any]) -> str:
    if not credential:
        return url
    location = str(config.get("auth_location") or "header").lower()
    if location == "query":
        param_name = config.get("query_param", "api_key")
        separator = "&" if urllib.parse.urlparse(url).query else "?"
        return f"{url}{separator}{urllib.parse.urlencode({param_name: credential})}"

    header_name = config.get("auth_header", "Authorization")
    if str(header_name).lower() == "authorization":
        scheme = config.get("auth_scheme", "Bearer")
        headers[header_name] = f"{scheme} {credential}" if scheme else credential
    else:
        headers[header_name] = credential
    return url


def build_mcp_request_config(
    binding: dict[str, Any],
    credential: Optional[str] = None,
) -> tuple[str, dict[str, str], int]:
    config = _parse_json_object(binding.get("config_json"))
    timeout_ms = int(config.get("timeout_ms") or config.get("timeout") or DEFAULT_MCP_TIMEOUT_MS)
    headers: dict[str, str] = {}
    for key, value in (config.get("headers") or {}).items():
        if key:
            headers[str(key)] = str(value)
    endpoint_url = binding.get("endpoint_url") or ""
    endpoint_url = _apply_auth(endpoint_url, headers, credential, config)
    headers.setdefault("Content-Type", "application/json")
    headers.setdefault("Accept", _MCP_ACCEPT)
    return endpoint_url, headers, timeout_ms


def discover_all_tools(
    endpoint_url: str,
    timeout_ms: int = DEFAULT_MCP_TIMEOUT_MS,
    headers: Optional[dict[str, str]] = None,
    client: Optional[httpx.Client] = None,
    validate_url: bool = True,
) -> list[dict[str, Any]]:
    if validate_url:
        endpoint_url = validate_mcp_server_url(endpoint_url)
    timeout_seconds = max(timeout_ms, 1000) / 1000.0
    client_headers = {"Content-Type": "application/json", "Accept": _MCP_ACCEPT}
    if headers:
        client_headers.update(headers)

    tools: list[dict[str, Any]] = []
    cursor = None
    request_id = 2
    close_client = client is None
    if client is None:
        client = httpx.Client(
            timeout=timeout_seconds, headers=client_headers, follow_redirects=False
        )
        # Standalone use (e.g. binding health check): establish a session first so
        # session-enforcing servers accept the tools/list call.
        _mcp_initialize(client, endpoint_url)
    try:
        while True:
            params = {}
            if cursor:
                params["cursor"] = cursor
            tools_result = _jsonrpc_request(
                client, endpoint_url, "tools/list", params, request_id=request_id
            )
            request_id += 1
            page_tools = (
                tools_result.get("tools", []) if isinstance(tools_result, dict) else []
            )
            if isinstance(page_tools, list):
                tools.extend([_normalize_tool(tool) for tool in page_tools])
            cursor = (
                tools_result.get("nextCursor")
                if isinstance(tools_result, dict)
                else None
            )
            if not cursor:
                break
    finally:
        if close_client:
            client.close()
    return tools


def discover_mcp_server(
    endpoint_url: str,
    timeout_ms: int = DEFAULT_MCP_TIMEOUT_MS,
    headers: Optional[dict[str, str]] = None,
) -> MCPDiscoveryResult:
    endpoint_url = validate_mcp_server_url(endpoint_url)
    timeout_seconds = max(timeout_ms, 1000) / 1000.0
    client_headers = {"Content-Type": "application/json", "Accept": _MCP_ACCEPT}
    if headers:
        client_headers.update(headers)

    with httpx.Client(
        timeout=timeout_seconds, headers=client_headers, follow_redirects=False
    ) as client:
        init_result = _mcp_initialize(client, endpoint_url)

        tools = discover_all_tools(
            endpoint_url,
            timeout_ms=timeout_ms,
            headers=headers,
            client=client,
            validate_url=False,
        )

        server_info = {}
        if isinstance(init_result, dict):
            server_info = init_result.get("serverInfo") or {}

        server_name = (
            (server_info or {}).get("name")
            or urlparse(endpoint_url).hostname
            or "mcp-server"
        )
        protocol_version = (
            (init_result or {}).get("protocolVersion")
            or DEFAULT_MCP_PROTOCOL_VERSION
        )

        capabilities = {}
        if isinstance(init_result, dict):
            capabilities = init_result.get("capabilities") or {}

        return MCPDiscoveryResult(
            server_name=server_name,
            protocol_version=protocol_version,
            capabilities=capabilities if isinstance(capabilities, dict) else {},
            tools=tools,
        )


def _normalize_tool(tool: Any) -> dict[str, Any]:
    if not isinstance(tool, dict):
        return {"name": str(tool)}
    name = tool.get("name") or tool.get("title") or tool.get("tool") or ""
    return {
        "name": name,
        "description": tool.get("description", ""),
        "input_schema": tool.get("inputSchema") or tool.get("input_schema") or {},
        "raw": tool,
    }


def execute_mcp_tool(
    endpoint_url: str,
    action: str,
    params: Optional[dict[str, Any]] = None,
    credential: Optional[str] = None,
    config_json: Optional[str] = None,
    transport_type: str = "streamable_http",
) -> MCPExecutionResult:
    if transport_type not in ("streamable_http", "http"):
        return MCPExecutionResult(
            success=False,
            error=f"Unsupported MCP transport: {transport_type}",
            error_code="UNSUPPORTED_TRANSPORT",
            transport=transport_type,
        )

    binding = {"endpoint_url": endpoint_url, "config_json": config_json}
    endpoint_url, headers, timeout_ms = build_mcp_request_config(
        binding, credential=credential
    )
    timeout_seconds = max(timeout_ms, 1000) / 1000.0

    with httpx.Client(timeout=timeout_seconds, headers=headers, follow_redirects=False) as client:
        try:
            # Establish a session before invoking the tool; session-enforcing
            # servers reject tools/call otherwise.
            _mcp_initialize(client, endpoint_url)
            result = _jsonrpc_request(
                client,
                endpoint_url,
                "tools/call",
                {
                    "name": action,
                    "arguments": params or {},
                },
                request_id=2,
            )
        except httpx.HTTPStatusError as e:
            return MCPExecutionResult(
                success=False,
                error=f"HTTP {e.response.status_code}: {e.response.text[:500]}",
                error_code="HTTP_ERROR",
                status=e.response.status_code,
                transport=transport_type,
            )
        except Exception as e:
            return MCPExecutionResult(
                success=False,
                error=str(e),
                error_code="EXECUTION_ERROR",
                transport=transport_type,
            )

    return MCPExecutionResult(
        success=True,
        body=result,
        status=200,
        transport=transport_type,
    )
