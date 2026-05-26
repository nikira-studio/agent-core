import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from app.connectors import register_connector
from app.security.url_validation import validate_public_url
from app.security.safe_http import safe_urlopen


class GenericHttpConnector:
    connector_type_id = "generic_http"

    def test_connection(self, credential, config_json: Optional[str]) -> dict:
        if hasattr(credential, "raw"):
            credential = credential.raw
        config = self._parse_config(config_json)
        test_url = config.get("test_url") or config.get("base_url")
        if not test_url:
            return {
                "success": False,
                "error": "Generic HTTP bindings need config_json.test_url or config_json.base_url",
            }
        result = self._call(
            credential=credential,
            config=config,
            params={"url": test_url, "method": config.get("test_method", "GET")},
        )
        if result.get("success"):
            return {
                "success": True,
                "status": result.get("status"),
                "body_preview": result.get("body_preview"),
            }
        return result

    def execute(
        self,
        action: str,
        params: dict,
        credential,
        config_json: Optional[str],
        session=None,
    ) -> dict:
        if hasattr(credential, "raw"):
            credential = credential.raw
        config = self._parse_config(config_json)
        merged_params = dict(params)
        if action != "call_endpoint":
            parts = action.strip().split(None, 1)
            method = parts[0].upper() if parts else "GET"
            path = parts[1] if len(parts) > 1 else ""
            if method not in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"}:
                return {"success": False, "error": f"Unknown action: {action}"}
            merged_params.setdefault("method", method)
            merged_params.setdefault("path", path)
            if "body" not in merged_params:
                _transport_keys = {"method", "path", "url", "headers", "query"}
                body = {
                    k: v for k, v in merged_params.items() if k not in _transport_keys
                }
                if body:
                    merged_params["body"] = body
        return self._call(credential, config, merged_params)

    def _parse_config(self, config_json: Optional[str]) -> dict:
        if not config_json:
            return {}
        try:
            parsed = json.loads(config_json)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _call(self, credential, config: dict, params: dict) -> dict:
        if hasattr(credential, "raw"):
            credential = credential.raw
        url = params.get("url") or self._join_url(
            config.get("base_url"), params.get("path")
        )
        if not url:
            return {
                "success": False,
                "error": "url or config_json.base_url plus params.path is required",
            }
        try:
            validate_public_url(url)
        except ValueError as e:
            return {"success": False, "error": str(e)}
        method = str(params.get("method") or "GET").upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            return {"success": False, "error": f"Unsupported method: {method}"}

        headers = {}
        headers.update(config.get("headers") or {})
        headers.update(params.get("headers") or {})
        headers.setdefault("Accept", "application/json")

        url = self._apply_auth(url, headers, credential, config)
        payload = params.get("body")
        data = None
        if payload is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(payload).encode("utf-8")

        try:
            req = urllib.request.Request(url, method=method, headers=headers, data=data)
            with safe_urlopen(req, timeout=int(config.get("timeout", 20))) as resp:
                raw = resp.read()
                text = raw.decode("utf-8", errors="replace") if raw else ""
                return {
                    "success": 200 <= resp.status < 300,
                    "status": resp.status,
                    "headers": dict(resp.headers.items()),
                    "body": self._decode_body(text),
                    "body_preview": text[:500],
                }
        except urllib.error.HTTPError as e:
            text = e.read().decode("utf-8", errors="replace")
            return {
                "success": False,
                "status": e.code,
                "error": f"HTTP {e.code}: {text[:500]}",
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def _apply_auth(self, url: str, headers: dict, credential, config: dict) -> str:
        if hasattr(credential, "raw"):
            credential = credential.raw
        location = config.get("auth_location", "header")
        if location == "query":
            param_name = config.get("query_param", "api_key")
            separator = "&" if urllib.parse.urlparse(url).query else "?"
            return f"{url}{separator}{urllib.parse.urlencode({param_name: credential})}"

        header_name = config.get("auth_header", "Authorization")
        if header_name.lower() == "authorization":
            scheme = config.get("auth_scheme", "Bearer")
            headers[header_name] = f"{scheme} {credential}" if scheme else credential
        else:
            headers[header_name] = credential
        return url

    def _join_url(self, base_url: Optional[str], path: Optional[str]) -> Optional[str]:
        if not base_url:
            return None
        if not path:
            return base_url
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    def _decode_body(self, text: str):
        if not text:
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text


register_connector("generic_http", GenericHttpConnector)
