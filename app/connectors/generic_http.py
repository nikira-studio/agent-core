import http.cookies
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from app.connectors import BaseConnector
from app.connectors.errors import ProviderError, SessionExpiredError
from app.connectors.value_utils import decode_json_body
from app.security.url_validation import validate_public_url
from app.security.safe_http import (
    CertificatePinError,
    ResponseTooLargeError,
    read_capped,
    safe_urlopen,
)

# A 401 this soon after signing in is not a stale session: the service refuses
# what this credential is allowed to read, and signing in again will not help.
_FRESH_LOGIN_SECONDS = 10


class GenericHttpConnector(BaseConnector):
    connector_type_id = "generic_http"

    def test_connection(self, credential, config_json: Optional[str]) -> dict:
        if hasattr(credential, "raw"):
            credential = credential.raw
        config = self._parse_config(config_json)
        if self._login_config(config):
            return self._test_login(credential, config)
        test_url = config.get("test_url") or config.get("base_url")
        if not test_url:
            return {
                "success": False,
                "error": "Generic HTTP bindings need config_json.test_url or config_json.base_url",
            }
        method = config.get("test_method", "GET").upper()
        if method not in {"GET", "HEAD", "OPTIONS"}:
            return {
                "success": False,
                "error": "Connection test method must be GET, HEAD or OPTIONS",
            }
        result = self._call(
            credential=credential,
            config=config,
            params={"url": test_url, "method": method},
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
        login = self._login_config(config)
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
        refused = self._check_allowed(config, merged_params, login=bool(login))
        if refused:
            return refused
        if "allowed" in config or login:
            # A fixed request list means the request is exactly what the binding
            # says: a caller's own headers (a method override, a different Host)
            # could turn an allowed read into something else.
            merged_params.pop("headers", None)
        if not login:
            return self._call(credential, config, merged_params)
        if not session or not session.get("cookies"):
            raise SessionExpiredError("Not signed in yet")
        result = self._call(credential, config, merged_params, session=session)
        if not result.get("success") and result.get("status") in (401, 403):
            if time.time() - float(session.get("at") or 0) < _FRESH_LOGIN_SECONDS:
                result["error"] = (
                    f"The service refused the request (HTTP {result['status']}) right after "
                    "signing in. The account may not be allowed to read this path."
                )
                return result
            raise SessionExpiredError("The service no longer accepts the session")
        return result

    # ─── login first, then call ───────────────────────────────────────────

    def wants_session(self, config: Optional[dict]) -> bool:
        return bool(self._login_config(config or {}))

    @staticmethod
    def _login_config(config: dict) -> Optional[dict]:
        login = config.get("login")
        if isinstance(login, dict) and isinstance(login.get("path"), str) and login["path"]:
            return login
        return None

    @staticmethod
    def _check_allowed(config: dict, params: dict, login: bool = False) -> Optional[dict]:
        """With an `allowed` list, only those exact requests are ever sent.

        A binding that signs in must have one: the session cookie is worth more
        than any single call, so it never goes with a request nobody listed.
        """
        allowed = config.get("allowed")
        if allowed is None and not login:
            return None
        if login and not (isinstance(allowed, list) and allowed):
            return {
                "success": False,
                "error": "This signing-in service needs a fixed list of allowed requests. "
                "Add an \"allowed\" list in the binding's Advanced settings.",
                "error_code": "REQUEST_NOT_ALLOWED",
            }
        listed = {str(entry).strip() for entry in allowed} if isinstance(allowed, list) else set()
        method = str(params.get("method") or "GET").upper()
        path = params.get("path")
        if params.get("url") or not isinstance(path, str) or f"{method} {path}" not in listed:
            return {
                "success": False,
                "error": "This binding only allows a fixed list of requests, and this is not one of them.",
                "error_code": "REQUEST_NOT_ALLOWED",
            }
        return None

    @staticmethod
    def _login_secrets(credential, config: dict, login: dict) -> dict:
        """The username and password, from a JSON credential or one 'user:password' string."""
        if hasattr(credential, "fields") and credential.fields:
            fields = dict(credential.fields)
        else:
            fields = {}
        raw = credential.raw if hasattr(credential, "raw") else credential
        separator = config.get("credential_split")
        if not fields.get("username") and separator and isinstance(raw, str) and separator in raw:
            username, password = raw.split(separator, 1)
            fields = {"username": username, "password": password}
        if not fields.get("username") or fields.get("password") is None:
            raise ProviderError(
                "The credential needs a username and a password (as JSON fields, or "
                "as one string split with credential_split).",
                error_code="INVALID_CREDENTIAL",
            )
        return fields

    def _login(self, credential, config: dict) -> dict:
        """Sign in and return {'cookies': {...}, 'at': epoch}. Never returns or logs a secret."""
        login = self._login_config(config)
        secrets = self._login_secrets(credential, config, login)
        body = login.get("body")
        if isinstance(body, dict):
            body = {
                key: self._fill(value, secrets) if isinstance(value, str) else value
                for key, value in body.items()
            }
        url = self._join_url(config.get("base_url"), login["path"])
        if not url:
            raise ProviderError("The binding needs a base_url to sign in to.", error_code="INVALID_CONFIGURATION")
        try:
            validate_public_url(url)
        except ValueError as e:
            raise ProviderError(str(e), error_code="BLOCKED_URL")
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, method="POST", headers=headers, data=data)
        try:
            with safe_urlopen(
                req, timeout=int(config.get("timeout", 20)), **self._pin_kwargs(config)
            ) as resp:
                cookies = self._cookies_from(resp.headers)
        except urllib.error.HTTPError as e:
            raise ProviderError(
                f"Signing in to the service failed (HTTP {e.code}). Check the stored credential.",
                error_code="LOGIN_FAILED",
            )
        except CertificatePinError as e:
            raise ProviderError(str(e), error_code="CERTIFICATE_CHANGED")
        except ProviderError:
            raise
        except Exception as e:
            raise ProviderError(f"Could not reach the service to sign in: {e}", error_code="LOGIN_FAILED")
        if not cookies:
            raise ProviderError(
                "The service accepted the sign-in but did not set a session cookie.",
                error_code="LOGIN_FAILED",
            )
        return {"cookies": cookies, "at": time.time()}

    def refresh_session(self, credential, config_json: Optional[str], current_session) -> dict:
        config = self._parse_config(config_json)
        return {"session": self._login(credential, config), "expires_at": None}

    def _test_login(self, credential, config: dict) -> dict:
        try:
            self._login(credential, config)
        except ProviderError as e:
            return {"success": False, "error": str(e), "error_code": e.error_code}
        return {"success": True, "status": "signed_in"}

    @staticmethod
    def _fill(template: str, secrets: dict) -> str:
        out = template
        for name in ("username", "password"):
            out = out.replace("{{credential.%s}}" % name, str(secrets.get(name, "")))
        return out

    @staticmethod
    def _cookies_from(headers) -> dict:
        jar: dict = {}
        for value in headers.get_all("Set-Cookie") or []:
            parsed = http.cookies.SimpleCookie()
            try:
                parsed.load(value)
            except http.cookies.CookieError:
                continue
            for name, morsel in parsed.items():
                if morsel.value:
                    jar[name] = morsel.value
        return jar

    @staticmethod
    def _pin_kwargs(config: dict) -> dict:
        tls = config.get("tls")
        pin = tls.get("pin_sha256") if isinstance(tls, dict) else None
        return {"tls_pin": str(pin)} if pin else {}

    def _parse_config(self, config_json: Optional[str]) -> dict:
        if not config_json:
            return {}
        try:
            parsed = json.loads(config_json)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}

    def _call(self, credential, config: dict, params: dict, session=None) -> dict:
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
        if params.get("url") and config.get("base_url"):
            # The binding's credential is for the configured host. A caller
            # may pick any path there, but not send the secret somewhere else.
            if not self._same_origin(url, config["base_url"]):
                return {
                    "success": False,
                    "error": "url must be on the binding's base_url host; use params.path for a different path",
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

        if session and session.get("cookies"):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in session["cookies"].items())
        elif not self._login_config(config):
            url = self._apply_auth(url, headers, credential, config)
        payload = params.get("body")
        data = None
        if payload is not None:
            headers.setdefault("Content-Type", "application/json")
            data = json.dumps(payload).encode("utf-8")

        try:
            req = urllib.request.Request(url, method=method, headers=headers, data=data)
            with safe_urlopen(
                req, timeout=int(config.get("timeout", 20)), **self._pin_kwargs(config)
            ) as resp:
                raw = read_capped(resp)
                text = raw.decode("utf-8", errors="replace") if raw else ""
                return {
                    "success": 200 <= resp.status < 300,
                    "status": resp.status,
                    "headers": {
                        k: v for k, v in resp.headers.items() if k.lower() != "set-cookie"
                    },
                    "body": self._decode_body(text),
                    "body_preview": text[:500],
                }
        except urllib.error.HTTPError as e:
            text = read_capped(e, 64 * 1024, truncate=True).decode("utf-8", errors="replace")
            return {
                "success": False,
                "status": e.code,
                "error": f"HTTP {e.code}: {text[:500]}",
            }
        except ResponseTooLargeError as e:
            return {"success": False, "error": str(e), "error_code": "RESPONSE_TOO_LARGE"}
        except CertificatePinError as e:
            return {"success": False, "error": str(e), "error_code": "CERTIFICATE_CHANGED"}
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

    @staticmethod
    def _same_origin(url: str, base_url: str) -> bool:
        a, b = urllib.parse.urlsplit(url), urllib.parse.urlsplit(base_url)
        default = {"http": 80, "https": 443}
        try:
            a_port = a.port or default.get(a.scheme)
            b_port = b.port or default.get(b.scheme)
        except ValueError:
            return False
        return (a.scheme, (a.hostname or "").lower(), a_port) == (
            b.scheme, (b.hostname or "").lower(), b_port
        )

    def _join_url(self, base_url: Optional[str], path: Optional[str]) -> Optional[str]:
        if not base_url:
            return None
        if not path:
            return base_url
        return f"{base_url.rstrip('/')}/{path.lstrip('/')}"

    def _decode_body(self, text: str):
        return decode_json_body(text)
