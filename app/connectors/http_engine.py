"""Declarative HTTP engine for adapter manifests."""

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

from app.connectors import BaseConnector
from app.connectors.base import Credential
from app.connectors.errors import (
    AuthExpiredError,
    RateLimitedError,
    SessionExpiredError,
)
from app.security.safe_http import safe_urlopen
from app.security.url_validation import validate_public_url


_RE_TEMPLATE = re.compile(
    r"\{\{\s*(params|cred|config)\.(\w+?)(?:\|(\w+)(?:\(([^)]*)\))?)?\s*\}\}"
)


class HttpEngine(BaseConnector):
    def __init__(self, connector_type: dict):
        self.ct = connector_type
        raw = connector_type.get("backend_json", "{}")
        if isinstance(raw, str):
            self.spec = json.loads(raw)
        else:
            self.spec = raw
        self.needs_session = bool(self.spec.get("session") or self.spec.get("refresh"))

    def execute(
        self,
        action: str,
        params: dict,
        credential: Credential,
        config_json: Optional[str],
        session: Optional[dict] = None,
    ) -> dict:
        request_def = self.spec.get("requests", {}).get(action)
        if not request_def:
            return {
                "success": False,
                "error": f"No request defined for action: {action}",
            }

        config = _parse_json(config_json)
        req = self._build_request(request_def, params, config)
        self._apply_auth(req, credential, session, config)
        self._apply_session(req, session)
        resp = self._send(req, config)
        self._raise_on_errors(resp)
        return self._extract(resp, request_def, config)

    def refresh_session(
        self,
        credential: Credential,
        config_json: Optional[str],
        current_session: Optional[dict],
    ) -> dict:
        session_spec = self.spec.get("session")
        refresh_spec = self.spec.get("refresh")
        if session_spec:
            return self._session_capture(credential, config_json, current_session)
        if refresh_spec:
            return self._oauth_refresh(credential, config_json, current_session)
        raise NotImplementedError

    # ─── request building ────────────────────────────────────────────────────

    def _build_request(self, request_def: dict, params: dict, config: dict) -> dict:
        method = request_def.get("method", "GET")
        path = self._render(request_def.get("path", ""), params, config)
        base_url = self._base_url(config)
        url = self._join_url(base_url, path)

        req = {"method": method, "url": url, "headers": {}, "body": None}

        for loc, key in [("query", "query_params"), ("header", "header_params")]:
            mapping = request_def.get(loc, {})
            if isinstance(mapping, dict):
                for param_name, param_loc in mapping.items():
                    if param_loc == "request_header":
                        val = self._render(
                            str(param_loc.get("default", "")), params, config
                        )
                        req["headers"][param_name] = val

        body_tpl = request_def.get("body", {}).get("template")
        if body_tpl:
            rendered = self._render(json.dumps(body_tpl), params, config)
            req["body"] = json.loads(rendered)

        return req

    def _base_url(self, config: dict) -> str:
        spec_base = self.spec.get("base_url", {})
        if isinstance(spec_base, dict):
            field_name = spec_base.get("field", "")
            if spec_base.get("from") == "config":
                return str(config.get(field_name, ""))
            return ""
        return str(spec_base) if spec_base else ""

    def _render(self, template: str, params: dict, config: dict) -> str:
        def replacer(m):
            src, key = m.group(1), m.group(2)
            filter_name = m.group(3)
            filter_arg = m.group(4)

            def _get_value():
                if src == "params":
                    return params.get(key, m.group(0))
                if src == "cred":
                    return self._cred_get(key, params, config)
                if src == "config":
                    return config.get(key, m.group(0))
                return m.group(0)

            val = _get_value()
            if filter_name == "default":
                if val is None or val == m.group(0):
                    type_map = {
                        "str": "",
                        "int": 0,
                        "float": 0.0,
                        "bool": False,
                        "list": [],
                    }
                    fallback = type_map.get(filter_arg, "") if filter_arg else ""
                    return str(fallback)
                return str(val)
            return str(val)

        return _RE_TEMPLATE.sub(replacer, template)

    def _cred_get(self, key: str, params: dict, config: dict) -> Any:
        parts = key.split(".", 1)
        field = parts[0]
        sub = parts[1] if len(parts) > 1 else None

        if field == "base64_credentials":
            username = self._cred_get("username", params, config) or ""
            password = self._cred_get("password", params, config) or ""
            import base64

            return base64.b64encode(f"{username}:{password}".encode()).decode()

        val = params.get("_cred", {}).get(field) or config.get(field)
        if sub and isinstance(val, dict):
            return val.get(sub)
        return val

    def _join_url(self, base: str, path: str) -> str:
        if not path:
            return base
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    # ─── auth ────────────────────────────────────────────────────────────────

    def _apply_auth(
        self, req: dict, credential: Credential, session, config: dict
    ) -> None:
        auth = self.spec.get("auth", {})
        auth_type = auth.get("type")
        if auth_type == "api_key":
            self._apply_api_key(req, auth, credential)
        elif auth_type == "bearer":
            self._apply_bearer(req, auth, credential)
        elif auth_type == "basic":
            self._apply_basic(req, auth, credential)
        elif auth_type == "custom_header":
            self._apply_custom_header(req, auth, credential)
        elif auth_type == "oauth2":
            self._apply_oauth2(req, auth, credential, session)
        else:
            if credential and credential.raw:
                req["headers"]["Authorization"] = f"Bearer {credential.raw}"

    def _apply_api_key(self, req: dict, auth: dict, credential: Credential) -> None:
        name = auth.get("name", "X-API-Key")
        loc = auth.get("location", "header")
        value = credential.raw or ""
        if loc == "query":
            sep = "&" if urllib.parse.urlparse(req["url"]).query else "?"
            req["url"] = f"{req['url']}{sep}{urllib.parse.urlencode({name: value})}"
        else:
            req["headers"][name] = value

    def _apply_bearer(self, req: dict, auth: dict, credential: Credential) -> None:
        token = credential.raw or ""
        req["headers"]["Authorization"] = f"Bearer {token}"

    def _apply_basic(self, req: dict, auth: dict, credential: Credential) -> None:
        username = credential.get("username", "")
        password = credential.get("password", "")
        import base64

        credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
        req["headers"]["Authorization"] = f"Basic {credentials}"

    def _apply_custom_header(
        self, req: dict, auth: dict, credential: Credential
    ) -> None:
        name = auth.get("name", "X-API-Key")
        value_template = auth.get("template", "{{ cred.raw }}")
        value = self._render(value_template, {"_cred": credential.fields}, {})
        req["headers"][name] = value

    def _apply_oauth2(
        self, req: dict, auth: dict, credential: Credential, session
    ) -> None:
        apply_spec = auth.get("apply", {})
        target = apply_spec.get("target")
        name = apply_spec.get("name", "Authorization")
        template = apply_spec.get("template", "Bearer {{ cred.access_token }}")
        if target == "request_header":
            access_token = (
                session.get("access_token")
                if session
                else credential.get("access_token")
            )
            rendered = self._render(template, {"_cred": credential.fields}, {})
            req["headers"][name] = rendered.replace(
                "{{ cred.access_token }}", str(access_token or "")
            )

    # ─── session ─────────────────────────────────────────────────────────────

    def _apply_session(self, req: dict, session: Optional[dict]) -> None:
        session_spec = self.spec.get("session")
        if not session_spec or not session:
            return
        apply_spec = session_spec.get("apply", {})
        if apply_spec.get("target") == "request_header":
            name = apply_spec.get("name")
            from_key = apply_spec.get("from", "session_id")
            if name and from_key:
                req["headers"][name] = str(session.get(from_key, ""))

    def _is_session_challenge(self, resp: Any) -> bool:
        session_spec = self.spec.get("session", {})
        trigger = session_spec.get("trigger", {})
        return trigger.get("http_status") == resp.status

    def _session_capture(
        self,
        credential: Credential,
        config_json: Optional[str],
        current_session: Optional[dict],
    ) -> dict:
        session_spec = self.spec.get("session", {})
        capture = session_spec.get("capture", {})
        source = capture.get("source")
        name = capture.get("name")
        as_key = capture.get("as", "session_id")
        request_def = self.spec.get("requests", {})
        first_action = next(iter(request_def.keys()), None)
        if not first_action:
            return {"session": current_session}
        config = _parse_json(config_json)
        req_tpl = request_def[first_action]
        req = self._build_request(req_tpl, {}, config)
        self._apply_auth(req, credential, current_session, config)
        try:
            resp = self._send(req, config)
        except Exception:
            return {"session": current_session}
        captured = {}
        if source == "response_header" and name:
            captured[as_key] = resp.headers.get(name, "")
        return {"session": {**(current_session or {}), **captured}}

    # ─── refresh ──────────────────────────────────────────────────────────────

    def _is_auth_expired(self, resp, session, credential: Credential) -> bool:
        refresh_spec = self.spec.get("refresh", {})
        trigger = refresh_spec.get("trigger", {})
        if trigger.get("http_status") == resp.status:
            return True
        cred_expires_at = credential.get("expires_at")
        if cred_expires_at and trigger.get("or_expired") == "cred.expires_at":
            try:
                expires_ts = float(cred_expires_at)
                if time.time() >= expires_ts:
                    return True
            except (ValueError, TypeError):
                pass
        return False

    def _oauth_refresh(
        self,
        credential: Credential,
        config_json: Optional[str],
        current_session: Optional[dict],
    ) -> dict:
        refresh_spec = self.spec.get("refresh", {})
        token_url = refresh_spec.get("token_url", "")
        grant = refresh_spec.get("grant", "refresh_token")
        response_map = refresh_spec.get("response_map", {})
        persist = refresh_spec.get("persist", {})

        fields = credential.fields
        refresh_token = fields.get("refresh_token") or fields.get("refresh_token")
        client_id = fields.get("client_id", "")
        client_secret = fields.get("client_secret", "")

        post_data = urllib.parse.urlencode(
            {
                "grant_type": grant,
                "refresh_token": refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            }
        ).encode("utf-8")

        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        req = urllib.request.Request(
            token_url, data=post_data, headers=headers, method="POST"
        )
        try:
            with safe_urlopen(req, timeout=30) as resp:
                raw = resp.read()
                token_resp = json.loads(raw.decode("utf-8"))
        except Exception as e:
            return {"error": str(e)}

        new_session = {**(current_session or {})}
        credential_update = {}

        for from_key, to_key in response_map.items():
            val = token_resp.get(from_key)
            if to_key == "access_token":
                new_session["access_token"] = val
            elif to_key == "expires_in":
                if val:
                    new_session["expires_at"] = str(time.time() + float(val))
            elif to_key.startswith("credential_if_present"):
                _, field_name = to_key.split(".", 1)
                if val:
                    credential_update[field_name] = val

        for from_key, to_key in persist.items():
            if from_key == "refresh_token" and token_resp.get("refresh_token"):
                credential_update["refresh_token"] = token_resp["refresh_token"]

        return {"session": new_session, "credential_update": credential_update}

    # ─── HTTP ─────────────────────────────────────────────────────────────────

    def _send(self, req: dict, config: dict) -> Any:
        headers = req.get("headers", {})
        headers.setdefault("Accept", "application/json")
        url = req["url"]
        body = None
        if req.get("body") is not None:
            headers.setdefault("Content-Type", "application/json")
            body = json.dumps(req["body"]).encode("utf-8")
        try:
            validate_public_url(url)
        except ValueError as e:
            raise Exception(f"Invalid URL: {e}")
        method = req.get("method", "GET")
        request = urllib.request.Request(url, method=method, headers=headers, data=body)
        timeout = float(config.get("timeout", 30))
        return safe_urlopen(request, timeout=timeout)

    def _raise_on_errors(self, resp) -> None:
        if resp.status == 429:
            retry_after = None
            ra = resp.headers.get("Retry-After")
            if ra:
                try:
                    retry_after = float(ra)
                except (ValueError, TypeError):
                    pass
            raise RateLimitedError(retry_after=retry_after)
        if self.spec.get("session") and self._is_session_challenge(resp):
            raise SessionExpiredError()
        if self.spec.get("refresh") and self._is_auth_expired(resp, None, None):
            raise AuthExpiredError()

    def _extract(self, resp, request_def: dict, config: dict) -> dict:
        resp_spec = request_def.get("response", {})
        raw = resp.read()
        text = raw.decode("utf-8", errors="replace") if raw else ""
        try:
            body = json.loads(text)
        except json.JSONDecodeError:
            body = text

        success_condition = resp_spec.get(
            "success_when", "$.status >= 200 and $.status < 300"
        )
        if success_condition.startswith("$.status"):
            is_success = 200 <= resp.status < 300
        else:
            is_success = self._evaluate_jsonpath(success_condition, body, resp)

        result = {
            "success": is_success,
            "status": resp.status,
            "body": body,
            "body_preview": text[:2000],
        }

        extract_path = resp_spec.get("extract")
        if extract_path:
            extracted = self._extract_jsonpath(extract_path, body, resp)
            result["body"] = extracted

        return result

    def _evaluate_jsonpath(self, condition: str, body: Any, resp) -> bool:
        if "result == " in condition:
            try:
                m = re.search(r"\.result\s*==\s*'([^']*)'", condition)
                if m and isinstance(body, dict):
                    return body.get("result") == m.group(1)
            except Exception:
                pass
        return 200 <= resp.status < 300

    def _extract_jsonpath(self, path: str, body: Any, resp) -> Any:
        if path.startswith("$."):
            parts = path[2:].split(".")
            current = body
            for part in parts:
                if isinstance(current, dict):
                    current = current.get(part)
                else:
                    return None
            return current
        return body


def _parse_json(config_json: Optional[str]) -> dict:
    if not config_json:
        return {}
    try:
        val = json.loads(config_json)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}
