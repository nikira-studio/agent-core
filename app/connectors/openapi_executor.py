import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from app.connectors import BaseConnector
from app.security.url_validation import validate_public_url

_MAX_RESPONSE_BODY = 50_000


class OpenApiExecutor(BaseConnector):
    connector_type_id = "__openapi__"

    def test_connection(
        self, credential: Optional[str], config_json: Optional[str]
    ) -> dict:
        spec = self._load_spec(config_json)
        if not spec:
            return {"success": False, "error": "No spec available for this connector"}

        servers = self._get_servers(spec)
        if not servers:
            return {"success": False, "error": "No server URL in spec"}

        config = {}
        if config_json:
            try:
                config = json.loads(config_json)
            except json.JSONDecodeError:
                pass

        base_url = config.get("base_url") or servers[0]
        try:
            validate_public_url(base_url)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        test_op = self._find_safe_test_op(spec)

        if test_op:
            try:
                path = self._fill_path_params(test_op["path"], {})
                url = f"{base_url}{path}"
                headers = {}
                url = self._apply_auth(spec, headers, credential, config_json, url)
                req = urllib.request.Request(url, method="GET", headers=headers)
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return {
                        "success": True,
                        "status": resp.status,
                        "message": f"Tested {test_op['operation_id']}: HTTP {resp.status}",
                    }
            except urllib.error.HTTPError as e:
                if 200 <= e.code < 500:
                    return {
                        "success": True,
                        "status": e.code,
                        "message": f"Server reachable (HTTP {e.code})",
                    }
                return {"success": False, "error": f"HTTP {e.code}"}
            except Exception:
                pass

        try:
            req = urllib.request.Request(base_url, method="HEAD")
            with urllib.request.urlopen(req, timeout=10) as resp:
                return {
                    "success": True,
                    "status": resp.status,
                    "message": f"Server reachable (HTTP {resp.status})",
                }
        except urllib.error.HTTPError as e:
            if e.code < 500:
                return {
                    "success": True,
                    "status": e.code,
                    "message": f"Server reachable (HTTP {e.code})",
                }
            return {"success": False, "error": f"HTTP {e.code}"}
        except Exception as e:
            return {"success": False, "error": f"Cannot reach server: {e}"}

    def execute(
        self,
        action: str,
        params: dict,
        credential: Optional[str],
        config_json: Optional[str],
    ) -> dict:
        spec = self._load_spec(config_json)
        if not spec:
            return {"success": False, "error": "No spec available"}

        operation = self._find_operation(spec, action)
        if not operation:
            return {"success": False, "error": f"Action not found in spec: {action}"}

        servers = self._get_servers(spec)
        if not servers:
            return {"success": False, "error": "No server URL in spec"}

        config = {}
        if config_json:
            try:
                config = json.loads(config_json)
            except json.JSONDecodeError:
                pass

        base_url = config.get("base_url") or servers[0]
        defaults = config.get("default_params", {})
        merged = {**defaults, **params}

        path = self._fill_path_params(operation["path"], merged)
        url = f"{base_url}{path}"

        query_params = {}
        header_params = {}
        body = None

        for p in operation.get("parameters", []):
            name = p["name"]
            location = p.get("in", "query")
            if name in merged:
                if location == "query":
                    query_params[name] = merged[name]
                elif location == "header":
                    header_params[name] = str(merged[name])
                elif location == "path":
                    pass

        if operation.get("request_body"):
            body = merged

        if query_params:
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            url = f"{url}{separator}{urllib.parse.urlencode(query_params)}"

        headers = {}
        headers.update(header_params)
        headers.setdefault("Accept", "application/json")
        url = self._apply_auth(spec, headers, credential, config_json, url) or url
        try:
            validate_public_url(url)
        except ValueError as e:
            return {"success": False, "error": str(e)}

        data = None
        if body is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(body).encode("utf-8")

        method = operation["method"]

        try:
            req = urllib.request.Request(url, method=method, headers=headers, data=data)
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace") if raw else ""
                body_obj = self._decode_body(text)
                truncated = False
                if body_obj is not None:
                    body_str = (
                        json.dumps(body_obj)
                        if not isinstance(body_obj, str)
                        else body_obj
                    )
                    if len(body_str) > _MAX_RESPONSE_BODY:
                        truncated = True
                        body_obj = self._decode_body(text[:_MAX_RESPONSE_BODY])
                return {
                    "success": 200 <= resp.status < 300,
                    "status": resp.status,
                    "body": body_obj,
                    "body_preview": text[:2000],
                    "truncated": truncated,
                }
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            return {
                "success": False,
                "status": e.code,
                "error": f"HTTP {e.code}: {text[:1000]}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _load_spec(self, config_json: Optional[str]) -> Optional[dict]:
        if not config_json:
            return None
        try:
            config = json.loads(config_json)
            return config.get("_operations_json")
        except json.JSONDecodeError:
            return None

    def _get_servers(self, spec: dict) -> list[str]:
        servers = spec.get("servers", [])
        if not isinstance(servers, list):
            return []
        result = []
        for s in servers:
            if isinstance(s, str) and s:
                result.append(s.rstrip("/"))
            elif isinstance(s, dict) and s.get("url"):
                result.append(s["url"].rstrip("/"))
        return result

    def _find_operation(self, spec: dict, action: str) -> Optional[dict]:
        for op in spec.get("operations", []):
            if op.get("operation_id") == action:
                return {
                    "operation_id": action,
                    "method": op["method"],
                    "path": op["path"],
                    "parameters": op.get("parameters", []),
                    "request_body": op.get("request_body"),
                    "summary": op.get("summary", ""),
                }
        return None

    def _find_safe_test_op(self, spec: dict) -> Optional[dict]:
        for op in spec.get("operations", []):
            if op["method"] != "GET":
                continue
            has_required_path_param = any(
                isinstance(p, dict) and p.get("in") == "path" and p.get("required")
                for p in op.get("parameters", [])
            )
            if not has_required_path_param:
                return {
                    "operation_id": op["operation_id"],
                    "method": "GET",
                    "path": op["path"],
                }
        return None

    def _fill_path_params(self, path: str, params: dict) -> str:
        def replacer(match):
            name = match.group(1)
            val = params.get(name, match.group(0))
            return urllib.parse.quote(str(val), safe="")

        return re.sub(r"\{(\w+)\}", replacer, path)

    def _apply_auth(
        self,
        spec: dict,
        headers: dict,
        credential: Optional[str],
        config_json: Optional[str],
        url: Optional[str] = None,
    ) -> Optional[str]:
        if not credential:
            return url

        config = {}
        if config_json:
            try:
                config = json.loads(config_json)
            except json.JSONDecodeError:
                pass

        if config.get("auth_mode") == "none":
            return url

        manual_header = config.get("auth_header")
        if manual_header:
            scheme = config.get("auth_scheme", "")
            if scheme:
                headers[manual_header] = f"{scheme} {credential}"
            else:
                headers[manual_header] = credential
            return url

        auth_scheme = self._select_auth_scheme(spec, config)
        if auth_scheme:
            return self._apply_auth_scheme(auth_scheme, headers, credential, url)

        auth_type = spec.get("auth_type", "bearer")
        if auth_type == "api_key":
            headers["X-API-Key"] = credential
        elif auth_type == "basic":
            headers["Authorization"] = f"Basic {credential}"
        else:
            headers["Authorization"] = f"Bearer {credential}"
        return url

    def _select_auth_scheme(self, spec: dict, config: dict) -> Optional[dict]:
        auth_schemes = spec.get("auth_schemes", [])
        if not isinstance(auth_schemes, list) or not auth_schemes:
            return None

        preferred_name = config.get("auth_scheme_name")
        if preferred_name:
            for scheme in auth_schemes:
                if isinstance(scheme, dict) and scheme.get("name") == preferred_name:
                    return scheme

        for scheme in auth_schemes:
            if isinstance(scheme, dict):
                return scheme
        return None

    def _apply_auth_scheme(
        self, scheme: dict, headers: dict, credential: str, url: Optional[str] = None
    ) -> Optional[str]:
        scheme_type = scheme.get("type")
        if scheme_type == "apiKey":
            location = (scheme.get("auth_location") or "header").lower()
            key_name = scheme.get("auth_header") or scheme.get("query_param") or "X-API-Key"
            if location == "query":
                if url is None:
                    return None
                separator = "&" if urllib.parse.urlparse(url).query else "?"
                return f"{url}{separator}{urllib.parse.urlencode({key_name: credential})}"
            if location == "cookie":
                existing = headers.get("Cookie")
                cookie_value = f"{key_name}={urllib.parse.quote(credential)}"
                headers["Cookie"] = f"{existing}; {cookie_value}" if existing else cookie_value
                return url
            headers[key_name] = credential
            return url

        if scheme_type == "http":
            http_scheme = (scheme.get("scheme") or "bearer").lower()
            if http_scheme == "basic":
                headers["Authorization"] = f"Basic {credential}"
            elif http_scheme == "bearer":
                headers["Authorization"] = f"Bearer {credential}"
            else:
                headers["Authorization"] = f"{http_scheme.title()} {credential}"
            return url

        if scheme_type in ("oauth2", "openIdConnect"):
            headers["Authorization"] = f"Bearer {credential}"
            return url

        headers["Authorization"] = f"Bearer {credential}"
        return url

    def _decode_body(self, text: str):
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text
