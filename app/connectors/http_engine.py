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
    r"\{\{\s*(params|cred|config)(?:\.(\w+?))?\s*(?:\s+\|\s*(\w+)(?:\(([^)]*)\))?)?\s*\}\}"
)

_OMIT = "__AGENT_CORE_OMIT__"

# Distinguishes "key absent" from "key present with value None" when resolving a
# single template token to its native Python type (see _resolve_token_raw).
_MISSING = object()


class HttpEngine(BaseConnector):
    def __init__(self, connector_type: dict):
        self.ct = connector_type
        raw = connector_type.get("backend_json", "{}")
        if isinstance(raw, str):
            self.spec = json.loads(raw)
        else:
            self.spec = raw
        self.needs_session = "session" in self.spec or "refresh" in self.spec

    def test_connection(self, credential: Credential, config_json: Optional[str]) -> dict:
        requests = self.spec.get("requests") or {}
        if not isinstance(requests, dict) or not requests:
            return {"success": False, "error": "No requests defined for this adapter"}

        preferred_actions = (
            self.spec.get("test_action"),
            "get_session_stats",
            "healthcheck",
            "test_connection",
            "ping",
        )
        action = next((name for name in preferred_actions if name in requests), None)
        if not action:
            action = next(iter(requests.keys()))

        try:
            session = None
            if self.spec.get("refresh"):
                refreshed = self.refresh_session(credential, config_json, None)
                session = refreshed.get("session")
            result = self.execute(action, {}, credential, config_json, session=session)
        except Exception as e:
            return {"success": False, "error": str(e)}

        if result.get("success"):
            output = {"success": True}
            if "status" in result:
                output["status"] = result["status"]
            if "body_preview" in result:
                output["body_preview"] = result["body_preview"]
            elif "body" in result:
                body = result["body"]
                if isinstance(body, str):
                    output["body_preview"] = body[:500]
                else:
                    output["body_preview"] = json.dumps(body)[:500]
            return output
        return result

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
        req = self._build_request(request_def, params, config, credential)
        self._apply_auth(req, credential, session, config)
        self._apply_session(req, session)
        resp = self._send(req, config)

        # Internal challenge_retry: per-request handshake (e.g. Transmission's
        # X-Transmission-Session-Id 409). The engine handles this transparently
        # up to max_retries times by capturing the new token from the response
        # and resending the same request. Only persistent failure escalates to
        # SessionExpiredError (via _raise_on_errors) for the execution layer.
        session_spec = self.spec.get("session", {})
        if session_spec.get("type") == "challenge_retry":
            max_retries = int(session_spec.get("max_retries", 1))
            attempts = 0
            while attempts < max_retries and self._is_session_challenge(resp):
                captured = self._capture_from_response(resp, session_spec)
                if not captured:
                    break
                session = {**(session or {}), **captured}
                req = self._build_request(request_def, params, config, credential)
                self._apply_auth(req, credential, session, config)
                self._apply_session(req, session)
                resp = self._send(req, config)
                attempts += 1

        self._raise_on_errors(resp, credential)
        return self._extract(resp, request_def, config)

    def _capture_from_response(self, resp, session_spec: dict) -> dict:
        """Pull the session token out of a challenge response per session.capture."""
        capture = session_spec.get("capture", {})
        source = capture.get("source")
        name = capture.get("name")
        as_key = capture.get("as", "session_id")
        if source == "response_header" and name:
            try:
                val = resp.headers.get(name)
            except Exception:
                val = None
            if val:
                return {as_key: val}
        return {}

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

    def _build_request(
        self,
        request_def: dict,
        params: dict,
        config: dict,
        cred: Optional[Credential] = None,
    ) -> dict:
        method = request_def.get("method", "GET")
        path = self._render(request_def.get("path", ""), params, config, cred)
        base_url = self._base_url(config)
        url = self._join_url(base_url, path)

        req = {"method": method, "url": url, "headers": {}, "body": None}

        for loc in ("query_params", "header_params"):
            mapping = request_def.get(loc, {})
            if not isinstance(mapping, dict):
                continue
            for param_name, param_tpl in mapping.items():
                if not isinstance(param_tpl, str):
                    continue
                val = self._render(param_tpl, params, config, cred)
                if not val or val == _OMIT:
                    continue
                if loc == "header_params":
                    req["headers"][param_name] = val
                else:
                    if val == param_tpl and param_tpl.startswith("{{"):
                        continue
                    sep = "&" if urllib.parse.urlparse(req["url"]).query else "?"
                    req["url"] = (
                        f"{req['url']}{sep}{urllib.parse.urlencode({param_name: val})}"
                    )

        body_tpl = request_def.get("body", {}).get("template")
        if body_tpl:
            if isinstance(body_tpl, dict):
                req["body"] = _render_dict(body_tpl, params, config, cred)
            elif isinstance(body_tpl, str):
                rendered = self._render(body_tpl, params, config, cred)
                if rendered.startswith(("{", "[")):
                    try:
                        req["body"] = json.loads(rendered)
                    except json.JSONDecodeError:
                        req["body"] = rendered
                else:
                    req["body"] = rendered
            else:
                req["body"] = body_tpl

        return req

    def _base_url(self, config: dict) -> str:
        spec_base = self.spec.get("base_url", {})
        if isinstance(spec_base, dict):
            field_name = spec_base.get("field", "")
            if spec_base.get("from") == "config":
                return str(config.get(field_name, ""))
            return ""
        return str(spec_base) if spec_base else ""

    def _render(
        self,
        template: str,
        params: dict,
        config: dict,
        cred: Optional[Credential] = None,
    ) -> str:
        def replacer(m):
            src, key = m.group(1), m.group(2)
            filter_name = m.group(3)
            filter_arg = m.group(4)

            def _get_value():
                if src == "params":
                    if key:
                        return params.get(key, m.group(0))
                    return params
                if src == "cred":
                    # No-key cred tokens resolve through _cred_get (yields None,
                    # leaving the placeholder) rather than falling back to the
                    # params dict. Matches _render_value / _resolve_token_raw.
                    return self._cred_get(key, params, config, cred)
                if src == "config":
                    if key:
                        return config.get(key, m.group(0))
                    return config
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
                        "omit": _OMIT,
                    }
                    fallback_str = ""
                    type_arg = filter_arg
                    if filter_arg and ", as=" in filter_arg:
                        parts = filter_arg.split(", as=", 1)
                        fallback_str = parts[0]
                        type_arg = parts[1] if len(parts) > 1 else ""
                    if type_arg == "omit":
                        return _OMIT
                    if fallback_str:
                        try:
                            return str(json.loads(fallback_str))
                        except (json.JSONDecodeError, ValueError):
                            return fallback_str
                    if type_arg and type_arg in type_map:
                        return str(type_map[type_arg])
                    return ""
                return str(val)
            if filter_name == "rfc822_base64url":
                return _make_rfc822_base64url(params) if isinstance(params, dict) else m.group(0)
            if val is None:
                return m.group(0)
            return str(val)

        return _RE_TEMPLATE.sub(replacer, template)

    def _cred_get(
        self, key: str, params: dict, config: dict, cred: Optional[Credential] = None
    ) -> Any:
        return _cred_get_impl(key, params, config, cred)

    def _join_url(self, base: str, path: str) -> str:
        if not path:
            return base
        # An absolute URL in the path overrides base_url, so one adapter can span
        # multiple API hosts (e.g. Google Workspace: gmail.googleapis.com,
        # sheets.googleapis.com, docs.googleapis.com, people.googleapis.com).
        if path.startswith(("http://", "https://")):
            return path
        return f"{base.rstrip('/')}/{path.lstrip('/')}"

    # ─── auth ────────────────────────────────────────────────────────────────

    def _apply_auth(
        self, req: dict, credential: Credential, session, config: Optional[dict] = None
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
            if credential is not None and getattr(credential, "raw", None) is not None:
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
        cred_params = {"raw": credential.raw, **credential.fields}
        value = self._render(value_template, {"_cred": cred_params}, {})
        req["headers"][name] = value

    def _apply_oauth2(
        self, req: dict, auth: dict, credential: Credential, session
    ) -> None:
        apply_spec = auth.get("apply", {})
        target = apply_spec.get("target")
        name = apply_spec.get("name", "Authorization")
        template = apply_spec.get("template", "Bearer {{ cred.access_token }}")
        if target == "request_header":
            # The session's token (set by a refresh) must win over the stored
            # credential token, which may be stale. Substitute access_token
            # FIRST, then render any other cred.* placeholders — rendering the
            # whole template first would bake in the old credential token and
            # the later .replace would be a no-op (the placeholder is gone).
            access_token = (
                session.get("access_token")
                if session
                else credential.get("access_token")
            )
            header = template.replace("{{ cred.access_token }}", str(access_token or ""))
            req["headers"][name] = self._render(header, {"_cred": credential.fields}, {})

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
        # This check runs AFTER the response. A 2xx means the token worked, so a
        # stale local cred.expires_at must NOT flag it as expired — otherwise a
        # successful retry (after a refresh) gets re-raised as AuthExpired and
        # the call loops/fails once the original token's clock time passes.
        if 200 <= resp.status < 300:
            return False
        cred_expires_at = credential.get("expires_at") if credential else None
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
        refresh_token = fields.get("refresh_token")
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
        except urllib.error.HTTPError as e:
            try:
                error_data = json.loads(e.read().decode("utf-8"))
            except Exception:
                error_data = {}
            message = error_data.get("error_description") or error_data.get("error") or str(e)
            raise AuthExpiredError(f"OAuth refresh failed: {message}") from e
        except Exception as e:
            raise AuthExpiredError(f"OAuth refresh failed: {e}") from e

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

        if not new_session.get("access_token"):
            message = token_resp.get("error_description") or token_resp.get("error")
            raise AuthExpiredError(
                f"OAuth refresh failed: {message or 'provider did not return an access token'}"
            )

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
        try:
            return safe_urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as e:
            return _HTTPErrorResponse(e)

    def _raise_on_errors(self, resp, credential: Optional[Credential] = None) -> None:
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
        if self.spec.get("refresh") and self._is_auth_expired(resp, None, credential):
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


class _HTTPErrorResponse:
    """Minimal urllib response wrapper for HTTPError status responses.

    Transmission-style challenge flows return HTTP 409 with a session token in a
    response header. The engine needs to inspect that response like a normal
    response instead of crashing on urllib's exception path.
    """

    def __init__(self, error: urllib.error.HTTPError):
        self.status = error.code
        self.headers = error.headers
        self._body = error.read()

    def read(self) -> bytes:
        return self._body


def _parse_json(config_json: Optional[str]) -> dict:
    if not config_json:
        return {}
    try:
        val = json.loads(config_json)
        return val if isinstance(val, dict) else {}
    except json.JSONDecodeError:
        return {}


def _render_dict(
    template: Any, params: dict, config: dict, cred: Optional[Credential] = None
) -> Any:
    if isinstance(template, dict):
        rendered_dict = {}
        for k, v in template.items():
            rendered = _render_dict(v, params, config, cred)
            if rendered == _OMIT:
                continue
            rendered_dict[k] = rendered
        return rendered_dict
    if isinstance(template, list):
        rendered_items = []
        for item in template:
            rendered = _render_dict(item, params, config, cred)
            if rendered == _OMIT:
                continue
            try:
                rendered_items.append(json.loads(rendered))
            except (json.JSONDecodeError, TypeError):
                rendered_items.append(rendered)
        return rendered_items
    if isinstance(template, str):
        # A template that is exactly one token (no surrounding text) resolves to
        # the value's native Python type, so a numeric-looking STRING (e.g. a
        # pagination cursor like "560752") is not coerced to int by the JSON
        # round-trip below. Interpolated strings ("hello {{name}}") still take
        # the render-then-reparse path so embedded substitutions work.
        pure = _RE_TEMPLATE.fullmatch(template)
        if pure:
            return _resolve_token_raw(pure, params, config, cred)
        rendered = _RE_TEMPLATE.sub(
            lambda m: _render_value(m, params, config, cred), template
        )
        if rendered == _OMIT:
            return _OMIT
        try:
            return json.loads(rendered)
        except (json.JSONDecodeError, TypeError):
            return rendered
    return template


def _resolve_token_raw(
    m, params: dict, config: dict, cred: Optional[Credential] = None
) -> Any:
    """Resolve a single template token to its native Python value, preserving
    type. Mirrors _render_value's filter semantics but returns the real value
    (str stays str, list stays list, bool/int keep their type) rather than a
    stringified form. Returns _OMIT to drop the key, or the literal placeholder
    when an unfiltered token references a missing value (matching prior behavior
    so required-but-absent params surface in upstream validation)."""
    src, key = m.group(1), m.group(2)
    filter_name = m.group(3)
    filter_arg = m.group(4)

    if src == "params":
        val = params.get(key, _MISSING) if key else params
    elif src == "cred":
        resolved = _cred_get_impl(key, params, config, cred)
        val = _MISSING if resolved is None else resolved
    elif src == "config":
        val = config.get(key, _MISSING) if key else config
    else:
        val = _MISSING

    unset = val is _MISSING or val is None

    if filter_name == "default":
        if not unset:
            return val
        fallback_str = ""
        type_arg = filter_arg
        if filter_arg and ", as=" in filter_arg:
            parts = filter_arg.split(", as=", 1)
            fallback_str = parts[0]
            type_arg = parts[1] if len(parts) > 1 else ""
        if type_arg == "omit":
            return _OMIT
        if fallback_str:
            try:
                return json.loads(fallback_str)
            except (json.JSONDecodeError, ValueError):
                return fallback_str
        type_map = {"str": "", "int": 0, "float": 0.0, "bool": False, "list": []}
        if type_arg and type_arg in type_map:
            return type_map[type_arg]
        return ""
    if filter_name == "rfc822_base64url":
        return _make_rfc822_base64url(val) if isinstance(val, dict) else m.group(0)
    if unset:
        return m.group(0)
    return val


# Gmail's raw-message hard limit is ~35 MB; reject above this with a clear error
# rather than letting Google return an opaque 4xx.
_RFC822_MAX_BYTES = 35 * 1024 * 1024


def _make_rfc822_base64url(p: dict) -> str:
    """Build a Gmail raw (base64url) message from a params dict.

    Single-part (no ``attachments``) output is byte-identical to the original
    inline builder. When ``attachments`` is present, emit a ``multipart/mixed``
    message with the text body first and each attachment as a part. Each
    attachment is ``{filename, content_base64, mime_type?}`` (inline base64).
    """
    import base64

    def _addr(v):
        return ", ".join(v) if isinstance(v, list) else str(v)

    headers = [f"To: {_addr(p.get('to', []))}"]
    if p.get("cc"):
        headers.append(f"Cc: {_addr(p['cc'])}")
    if p.get("bcc"):
        headers.append(f"Bcc: {_addr(p['bcc'])}")
    if p.get("subject"):
        headers.append(f"Subject: {p['subject']}")
    if p.get("in_reply_to"):
        headers.append(f"In-Reply-To: {p['in_reply_to']}")
        headers.append(f"References: {p.get('references') or p['in_reply_to']}")
    body = p.get("body", "") or ""

    attachments = p.get("attachments")
    if not attachments:
        raw = ("\r\n".join(headers) + "\r\n\r\n" + body).encode("utf-8")
        if len(raw) > _RFC822_MAX_BYTES:
            raise ValueError("Email exceeds Gmail's ~35MB raw message limit")
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")

    from email.mime.base import MIMEBase
    from email.mime.multipart import MIMEMultipart
    from email.mime.text import MIMEText
    from email import encoders

    msg = MIMEMultipart("mixed")
    msg["To"] = _addr(p.get("to", []))
    if p.get("cc"):
        msg["Cc"] = _addr(p["cc"])
    if p.get("bcc"):
        msg["Bcc"] = _addr(p["bcc"])
    if p.get("subject"):
        msg["Subject"] = p["subject"]
    if p.get("in_reply_to"):
        msg["In-Reply-To"] = p["in_reply_to"]
        msg["References"] = p.get("references") or p["in_reply_to"]
    msg.attach(MIMEText(body, "plain", "utf-8"))

    for att in attachments:
        if not isinstance(att, dict):
            continue
        filename = att.get("filename") or "attachment"
        mime_type = att.get("mime_type") or "application/octet-stream"
        maintype, _, subtype = mime_type.partition("/")
        try:
            data = base64.b64decode(att.get("content_base64") or "")
        except Exception:
            raise ValueError(f"Attachment '{filename}' has invalid base64 content")
        part = MIMEBase(maintype or "application", subtype or "octet-stream")
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    raw = msg.as_bytes()
    if len(raw) > _RFC822_MAX_BYTES:
        raise ValueError("Email exceeds Gmail's ~35MB raw message limit")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("utf-8")


def _render_value(
    m, params: dict, config: dict, cred: Optional[Credential] = None
) -> str:
    src, key = m.group(1), m.group(2)
    filter_name = m.group(3)
    filter_arg = m.group(4)

    def _get_value():
        if src == "params":
            return params.get(key) if key else params
        if src == "cred":
            return _cred_get_impl(key, params, config, cred)
        if src == "config":
            return config.get(key, m.group(0)) if key else config
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
                "omit": _OMIT,
            }
            fallback_str = ""
            type_arg = filter_arg
            if filter_arg and ", as=" in filter_arg:
                parts = filter_arg.split(", as=", 1)
                fallback_str = parts[0]
                type_arg = parts[1] if len(parts) > 1 else ""
            if type_arg == "omit":
                return _OMIT
            # An explicit fallback wins over the type's zero-value, so
            # default(1, as=int) yields 1 (not 0). (Matches HttpEngine._render.)
            if fallback_str:
                try:
                    return str(json.loads(fallback_str))
                except (json.JSONDecodeError, ValueError):
                    return fallback_str
            if type_arg and type_arg in type_map:
                return str(type_map[type_arg])
            return ""
        return _stringify_for_template(val)
    if filter_name == "rfc822_base64url":
        return _make_rfc822_base64url(val) if isinstance(val, dict) else m.group(0)
    return _stringify_for_template(val)


def _stringify_for_template(val: Any) -> str:
    """Render a resolved value into the template stream so that
    `_render_dict`'s outer json.loads round-trips the type. Plain strings
    must stay quote-free (so 'hello {{name}}' works for interpolation);
    non-string types are emitted as JSON so True/42/[1,2] round-trip to
    bool/int/list rather than ending up as the literal strings 'True' etc."""
    if isinstance(val, str):
        return val
    return json.dumps(val)


def _cred_get_impl(
    key: str, params: dict, config: dict, cred: Optional[Credential] = None
) -> Any:
    parts = key.split(".", 1)
    field = parts[0]
    sub = parts[1] if len(parts) > 1 else None

    if field == "raw":
        if cred is not None:
            return cred.raw
        return params.get("_cred", {}).get("raw")

    if field == "base64_credentials":
        import base64

        username = _cred_get_impl("username", params, config, cred) or ""
        password = _cred_get_impl("password", params, config, cred) or ""
        return base64.b64encode(f"{username}:{password}".encode()).decode()

    if cred is not None and field in cred.fields:
        val = cred.fields[field]
        if sub and isinstance(val, dict):
            return val.get(sub)
        return val

    val = params.get("_cred", {}).get(field) or config.get(field)
    if sub and isinstance(val, dict):
        return val.get(sub)
    return val
