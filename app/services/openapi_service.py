import json
import re
import urllib.request
import urllib.error
import urllib.parse
from typing import Optional

from app.security.url_validation import validate_public_url

_MAX_SPEC_SIZE = 15 * 1024 * 1024
_FETCH_TIMEOUT = 30
_MAX_REDIRECTS = 3


def _slugify(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower().strip())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug[:64] or "unnamed-api"


def fetch_spec(url: str) -> str:
    validate_public_url(url)
    req = urllib.request.Request(url, method="GET")
    req.add_header("Accept", "application/json, application/yaml, text/yaml, */*")
    req.add_header("User-Agent", "AgentCore/1.0")
    try:
        with urllib.request.urlopen(req, timeout=_FETCH_TIMEOUT) as resp:
            if resp.headers.get("Content-Length"):
                size = int(resp.headers["Content-Length"])
                if size > _MAX_SPEC_SIZE:
                    raise ValueError(
                        f"Spec too large: {size} bytes (max {_MAX_SPEC_SIZE})"
                    )
            body = resp.read(_MAX_SPEC_SIZE + 1)
            if len(body) > _MAX_SPEC_SIZE:
                raise ValueError(f"Spec too large (max {_MAX_SPEC_SIZE} bytes)")
            return body.decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} fetching spec: {e.reason}")
    except urllib.error.URLError as e:
        raise ValueError(f"Failed to fetch spec: {e.reason}")


def _sanitize_for_json(obj):
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_sanitize_for_json(item) for item in obj]
    elif isinstance(obj, (str, int, float, bool, type(None))):
        return obj
    else:
        return str(obj)


def parse_spec(raw: str) -> tuple[dict, str]:
    raw = raw.strip()
    if raw.startswith("{"):
        spec = json.loads(raw)
        spec_json = raw
    else:
        try:
            import yaml

            spec = yaml.safe_load(raw)
        except ImportError:
            raise ValueError("YAML specs require PyYAML. Install it or provide JSON.")
        if not isinstance(spec, dict):
            raise ValueError("YAML content did not produce a valid spec object")
        spec = _sanitize_for_json(spec)
        spec_json = json.dumps(spec)

    if not isinstance(spec, dict):
        raise ValueError("Invalid spec: expected a JSON/YAML object")

    if "openapi" in spec:
        return _parse_oas3(spec), spec_json
    elif "swagger" in spec:
        return _parse_swagger2(spec), spec_json
    else:
        raise ValueError(
            "Unrecognized spec format: missing 'openapi' or 'swagger' version"
        )


def _extract_servers(spec: dict) -> list[str]:
    servers = []
    if "servers" in spec:
        for s in spec["servers"]:
            url = s.get("url", "")
            if url:
                if url.startswith("/"):
                    servers.append(url)
                else:
                    servers.append(url.rstrip("/"))
    elif "host" in spec:
        schemes = spec.get("schemes", ["https"])
        base_path = spec.get("basePath", "").rstrip("/")
        for scheme in schemes:
            servers.append(f"{scheme}://{spec['host']}{base_path}")
    return servers


def _normalize_auth_scheme(name: str, scheme: dict) -> Optional[dict]:
    if not isinstance(scheme, dict):
        return None

    scheme_type = scheme.get("type", "")
    if scheme_type == "http":
        http_scheme = (scheme.get("scheme") or "bearer").lower()
        return {
            "name": name,
            "type": "http",
            "scheme": http_scheme,
            "auth_location": "header",
            "auth_header": "Authorization",
        }
    if scheme_type == "apiKey":
        location = (scheme.get("in") or "header").lower()
        auth_name = scheme.get("name") or "X-API-Key"
        return {
            "name": name,
            "type": "apiKey",
            "scheme": "apiKey",
            "auth_location": location,
            "auth_header": auth_name,
            "query_param": auth_name,
            "cookie_name": auth_name,
        }
    if scheme_type in ("oauth2", "openIdConnect"):
        return {
            "name": name,
            "type": scheme_type,
            "scheme": "bearer",
            "auth_location": "header",
            "auth_header": "Authorization",
        }
    return None


def _extract_auth_schemes(spec: dict) -> list[dict]:
    security_schemes = {}
    components = spec.get("components", {})
    if isinstance(components, dict):
        schemes = components.get("securitySchemes", {})
        if isinstance(schemes, dict):
            security_schemes.update(schemes)

    definitions = spec.get("securityDefinitions", {})
    if isinstance(definitions, dict):
        security_schemes.update(definitions)

    normalized = []
    for name, scheme in security_schemes.items():
        entry = _normalize_auth_scheme(name, scheme)
        if entry:
            normalized.append(entry)
    return normalized


def _extract_auth_type(spec: dict) -> str:
    auth_schemes = _extract_auth_schemes(spec)
    if not auth_schemes:
        return "none"

    first = auth_schemes[0]
    if first["type"] == "http":
        return "bearer" if first.get("scheme") == "bearer" else "basic"
    if first["type"] == "apiKey":
        return "api_key"
    if first["type"] in ("oauth2", "openIdConnect"):
        return "bearer"

    return "none"


def _resolve_ref(spec: dict, ref: str):
    if not ref.startswith("#/"):
        return None
    parts = ref[2:].split("/")
    node = spec
    for part in parts:
        part = part.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict) and part in node:
            node = node[part]
        elif isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        else:
            return None
    return node


def _resolve_schema(spec: dict, schema: dict) -> dict:
    if not isinstance(schema, dict):
        return schema
    if "$ref" in schema:
        resolved = _resolve_ref(spec, schema["$ref"])
        if resolved:
            return _resolve_schema(spec, resolved)
        return {"type": "string", "description": f"Unresolved $ref: {schema['$ref']}"}
    if "allOf" in schema:
        merged = {}
        for sub in schema["allOf"]:
            resolved = _resolve_schema(spec, sub)
            if isinstance(resolved, dict):
                merged.update(resolved)
        return merged
    return schema


def _extract_params(spec: dict, op: dict) -> list[dict]:
    params = []
    for p in op.get("parameters", []):
        if "$ref" in p:
            p = _resolve_ref(spec, p["$ref"]) or p
        if not isinstance(p, dict):
            continue
        schema = p.get("schema", {})
        schema = _resolve_schema(spec, schema)
        params.append(
            {
                "name": p.get("name", ""),
                "in": p.get("in", "query"),
                "required": p.get("required", False),
                "description": p.get("description", ""),
                "schema": schema,
            }
        )
    return params


def _extract_body(spec: dict, op: dict) -> Optional[dict]:
    if "requestBody" in op:
        rb = op["requestBody"]
        if "$ref" in rb:
            rb = _resolve_ref(spec, rb["$ref"]) or rb
        content = rb.get("content", {})
        for media_type, media_def in content.items():
            schema = media_def.get("schema", {})
            schema = _resolve_schema(spec, schema)
            return {
                "required": rb.get("required", False),
                "content_type": media_type,
                "schema": schema,
            }
    body_param = None
    for p in op.get("parameters", []):
        if isinstance(p, dict) and p.get("in") == "body":
            body_param = p
            break
    if body_param:
        schema = body_param.get("schema", {})
        schema = _resolve_schema(spec, schema)
        return {
            "required": body_param.get("required", False),
            "content_type": "application/json",
            "schema": schema,
        }
    return None


def _extract_operations(spec: dict) -> list[dict]:
    operations = []
    seen_ids = set()
    paths = spec.get("paths", {})
    for path, path_item in paths.items():
        if not isinstance(path_item, dict):
            continue
        for method in ("get", "post", "put", "patch", "delete", "options", "head"):
            op = path_item.get(method)
            if not op or not isinstance(op, dict):
                continue
            if op.get("deprecated"):
                continue

            op_id = op.get("operationId")
            if not op_id:
                op_id = (
                    f"{method}_{path}".replace("/", "_")
                    .replace("{", "")
                    .replace("}", "")
                    .strip("_")
                )
                op_id = re.sub(r"_+", "_", op_id)

            if op_id in seen_ids:
                suffix = 2
                while f"{op_id}_{suffix}" in seen_ids:
                    suffix += 1
                op_id = f"{op_id}_{suffix}"
            seen_ids.add(op_id)

            all_params = _extract_params(spec, path_item) + _extract_params(spec, op)
            seen_names = set()
            deduped = []
            for p in all_params:
                key = (p["name"], p["in"])
                if key not in seen_names:
                    seen_names.add(key)
                    deduped.append(p)

            body = _extract_body(spec, op)

            operations.append(
                {
                    "operation_id": op_id,
                    "method": method.upper(),
                    "path": path,
                    "summary": op.get("summary", ""),
                    "description": op.get("description", ""),
                    "parameters": deduped,
                    "request_body": body,
                    "tags": op.get("tags", []),
                }
            )

    return operations


def _parse_oas3(spec: dict) -> dict:
    operations = _extract_operations(spec)
    servers = _extract_servers(spec)
    auth_type = _extract_auth_type(spec)
    auth_schemes = _extract_auth_schemes(spec)
    info = spec.get("info", {})

    return {
        "version": spec.get("openapi", "3.0"),
        "title": info.get("title", "Untitled API"),
        "description": info.get("description", ""),
        "api_version": info.get("version", ""),
        "servers": servers,
        "auth_type": auth_type,
        "auth_schemes": auth_schemes,
        "operations": operations,
    }


def _parse_swagger2(spec: dict) -> dict:
    operations = _extract_operations(spec)
    servers = _extract_servers(spec)
    auth_type = _extract_auth_type(spec)
    auth_schemes = _extract_auth_schemes(spec)
    info = spec.get("info", {})

    return {
        "version": spec.get("swagger", "2.0"),
        "title": info.get("title", "Untitled API"),
        "description": info.get("description", ""),
        "api_version": info.get("version", ""),
        "servers": servers,
        "auth_type": auth_type,
        "auth_schemes": auth_schemes,
        "operations": operations,
    }


def generate_connector_id(title: str) -> str:
    base = _slugify(title)
    if not base:
        base = "imported-api"
    return base


def import_spec(
    raw_or_url: str, display_name: Optional[str] = None, is_url: bool = True
) -> dict:
    if is_url:
        raw = fetch_spec(raw_or_url)
        url = raw_or_url
    else:
        raw = raw_or_url
        url = None

    parsed, _spec_json = parse_spec(raw)

    if not parsed.get("servers"):
        raise ValueError(
            "Spec has no server/base URL defined. Cannot build requests without a target host."
        )
    for server_url in parsed["servers"]:
        if urllib.parse.urlparse(server_url).scheme in ("http", "https"):
            validate_public_url(server_url)

    conn_id = generate_connector_id(parsed["title"])
    name = display_name or parsed["title"]

    warnings = []
    for op in parsed["operations"]:
        if not op["summary"] and not op["description"]:
            warnings.append(f"{op['operation_id']}: no description")

    action_ids = [op["operation_id"] for op in parsed["operations"]]

    operations_meta = json.dumps(
        {
            "servers": parsed["servers"],
            "auth_type": parsed["auth_type"],
            "auth_schemes": parsed["auth_schemes"],
            "operations": parsed["operations"],
        }
    )

    return {
        "connector_type_id": conn_id,
        "display_name": name,
        "description": parsed["description"]
        or f"Imported from {url or 'uploaded spec'}",
        "auth_type": parsed["auth_type"],
        "auth_schemes": parsed["auth_schemes"],
        "supported_actions": action_ids,
        "servers": parsed["servers"],
        "operations": parsed["operations"],
        "operations_json": operations_meta,
        "spec_url": url,
        "warnings": warnings,
        "operation_count": len(parsed["operations"]),
    }


def generate_tools(
    connector_type_id: str,
    operations_json: str,
    disabled_actions: Optional[list[str]] = None,
    include_disabled: bool = False,
    query: Optional[str] = None,
    limit: int = 20,
    offset: int = 0,
) -> dict:
    try:
        meta = json.loads(operations_json)
    except json.JSONDecodeError:
        return {"tools": [], "total": 0}

    disabled_set = {
        action for action in (disabled_actions or []) if isinstance(action, str)
    }
    all_ops = meta.get("operations", [])
    auth_schemes = meta.get("auth_schemes", [])

    def summarize_auth() -> str:
        if not isinstance(auth_schemes, list) or not auth_schemes:
            return "none"
        first = auth_schemes[0]
        if len(auth_schemes) > 1:
            names = ", ".join(
                s.get("name", "unknown") for s in auth_schemes[:2] if isinstance(s, dict)
            )
            return f"multiple ({names})" if names else "multiple"
        scheme_type = first.get("type", "unknown")
        if scheme_type == "http":
            scheme = first.get("scheme", "bearer")
            header = first.get("auth_header", "Authorization")
            return f"{scheme} via {header}"
        if scheme_type == "apiKey":
            location = first.get("auth_location", "header")
            name = first.get("auth_header", first.get("query_param", "X-API-Key"))
            return f"api key in {location} {name}"
        return scheme_type

    def summarize_inputs(op: dict) -> str:
        required = []
        optional = []
        for p in op.get("parameters", []):
            if not isinstance(p, dict):
                continue
            name = p.get("name")
            if not name:
                continue
            if p.get("required"):
                required.append(name)
            else:
                optional.append(name)
        rb = op.get("request_body")
        if isinstance(rb, dict) and rb.get("schema", {}).get("properties"):
            body_props = list(rb["schema"]["properties"].keys())
            body_required = rb["schema"].get("required", [])
            if body_props:
                required.extend([name for name in body_required if name not in required])
                optional.extend(
                    [name for name in body_props if name not in required and name not in optional]
                )
        chunks = []
        if required:
            chunks.append("required: " + ", ".join(required[:4]))
        if optional:
            chunks.append("optional: " + ", ".join(optional[:4]))
        return " | ".join(chunks)

    auth_summary = summarize_auth()

    if query:
        q = query.lower()
        all_ops = [
            op
            for op in all_ops
            if q in op["operation_id"].lower()
            or q in (op.get("summary") or "").lower()
            or q in (op.get("description") or "").lower()
            or q in op["path"].lower()
        ]

    if not include_disabled:
        all_ops = [op for op in all_ops if op["operation_id"] not in disabled_set]

    total = len(all_ops)
    page = all_ops[offset : offset + limit]

    tools = []
    for op in page:
        tool_name = f"{connector_type_id}_{op['operation_id']}"
        desc = (
            op.get("summary") or op.get("description") or f"{op['method']} {op['path']}"
        )
        input_summary = summarize_inputs(op)
        if auth_summary and auth_summary != "none":
            desc = f"{desc} | Auth: {auth_summary}"
        if input_summary:
            desc = f"{desc} | {input_summary}"
        schema = _build_input_schema(op)
        tools.append(
            {
                "name": tool_name,
                "action": op["operation_id"],
                "method": op["method"],
                "path": op["path"],
                "description": desc,
                "enabled": op["operation_id"] not in disabled_set,
                "auth_summary": auth_summary,
                "inputSchema": schema,
            }
        )

    return {"tools": tools, "total": total}


def _build_input_schema(op: dict) -> dict:
    properties = {}
    required = []

    for p in op.get("parameters", []):
        name = p["name"]
        p_schema = p.get("schema", {})
        if not isinstance(p_schema, dict):
            p_schema = {"type": "string"}
        prop = dict(p_schema)
        if p.get("description"):
            prop["description"] = p["description"]
        if p.get("in"):
            prop["_in"] = p["in"]
        properties[name] = prop
        if p.get("required"):
            required.append(name)

    rb = op.get("request_body")
    if rb and isinstance(rb, dict):
        rb_schema = rb.get("schema", {})
        if isinstance(rb_schema, dict):
            for prop_name, prop_def in rb_schema.get("properties", {}).items():
                if prop_name not in properties:
                    properties[prop_name] = prop_def
            for req in rb_schema.get("required", []):
                if req not in required:
                    required.append(req)

    return {
        "type": "object",
        "properties": properties,
        "required": required,
    }
