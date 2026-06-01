"""Unit tests for the declarative HTTP engine."""

import io
import json
import urllib.error
from unittest.mock import MagicMock

import pytest

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine


# ─── Fixtures ────────────────────────────────────────────────────────────────


def make_ct(backend_json: dict) -> dict:
    return {"id": "test-connector", "backend_json": json.dumps(backend_json)}


def make_cred(raw: str = "", fields: dict | None = None) -> Credential:
    return Credential(raw=raw, fields=fields or {}, reference_name="test-ref")


# ─── Request Building & Templating ───────────────────────────────────────────


class TestHttpEngineTemplating:
    def test_render_params_simple(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render("hello {{ params.name }}", {"name": "world"}, {})
        assert result == "hello world"

    def test_render_params_missing(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render("hello {{ params.name }}", {}, {})
        assert result == "hello {{ params.name }}"

    def test_render_params_default_str(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render("hello {{ params.name | default('') }}", {}, {})
        assert result == "hello "

    def test_render_params_default_list(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render("ids={{ params.ids | default([], as=list) }}", {}, {})
        assert result == "ids=[]"

    def test_render_params_default_omit(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "{{ params.ids | default('', as=omit) }}",
            {},
            {},
        )
        assert result == "__AGENT_CORE_OMIT__"

    def test_render_params_default_int(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render("count={{ params.count | default(0, as=int) }}", {}, {})
        assert result == "count=0"

    def test_render_params_default_bool(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "flag={{ params.flag | default(false, as=bool) }}", {}, {}
        )
        assert result == "flag=False"

    def test_render_params_default_from_value(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "name={{ params.name | default('', as=str) }}",
            {"name": "Alice"},
            {},
        )
        assert result == "name=Alice"

    def test_render_config_simple(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "url={{ config.base_url }}", {}, {"base_url": "http://example.com"}
        )
        assert result == "url=http://example.com"

    def test_render_cred_raw(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        cred = make_cred(raw="secret-token")
        result = engine._render(
            "token={{ cred.raw }}",
            {},
            {},
            cred,
        )
        assert result == "token=secret-token"

    def test_render_cred_base64_credentials(self):
        from app.connectors.http_engine import _cred_get_impl
        from app.connectors.base import Credential
        import base64

        _engine = HttpEngine(make_ct({"requests": {}}))
        cred = Credential(raw=None, fields={"username": "user", "password": "pass"})
        result = _cred_get_impl("base64_credentials", {}, {}, cred)
        assert result == base64.b64encode(b"user:pass").decode()

    def test_render_multiple_placeholders(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "{{ params.a }} + {{ params.b }} = {{ config.op }}",
            {"a": "1", "b": "2"},
            {"op": "sum"},
        )
        assert result == "1 + 2 = sum"

    def test_render_whitelisted_only_no_rce(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "{{ params.path }}",
            {"path": "{{ evil }}"},
            {},
        )
        assert "{{ evil }}" in result

    def test_render_whitelisted_no_python_exec(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._render(
            "{{ params.code }}",
            {"code": "__import__('os').system('rm -rf /')"},
            {},
        )
        assert "exec" not in result

    def test_render_dict_omits_marked_fields(self):
        engine = HttpEngine(make_ct({"requests": {}}))
        result = engine._build_request(
            {
                "method": "POST",
                "path": "/rpc",
                "body": {
                    "template": {
                        "method": "torrent-get",
                        "arguments": {
                            "fields": ["id"],
                            "ids": "{{ params.ids | default('', as=omit) }}",
                        },
                    }
                },
            },
            {},
            {"base_url": "http://example.com"},
            make_cred(raw="", fields={"username": "user", "password": "pass"}),
        )
        assert "ids" not in result["body"]["arguments"]


# ─── Auth Application ─────────────────────────────────────────────────────────


class TestHttpEngineAuth:
    def test_apply_api_key_header(self):
        engine = HttpEngine(
            make_ct(
                {
                    "auth": {
                        "type": "api_key",
                        "name": "X-Token",
                        "location": "header",
                    },
                }
            )
        )
        req = {"headers": {}, "url": "http://example.com"}
        cred = make_cred(raw="my-token")
        engine._apply_api_key(req, engine.spec["auth"], cred)
        assert req["headers"]["X-Token"] == "my-token"

    def test_apply_api_key_query(self):
        engine = HttpEngine(
            make_ct(
                {
                    "auth": {"type": "api_key", "name": "api_key", "location": "query"},
                }
            )
        )
        req = {"headers": {}, "url": "http://example.com"}
        cred = make_cred(raw="my-token")
        engine._apply_api_key(req, engine.spec["auth"], cred)
        assert "api_key=my-token" in req["url"]

    def test_apply_bearer(self):
        engine = HttpEngine(make_ct({"auth": {"type": "bearer"}}))
        req = {"headers": {}}
        cred = make_cred(raw="jwt-token")
        engine._apply_bearer(req, {}, cred)
        assert req["headers"]["Authorization"] == "Bearer jwt-token"

    def test_apply_basic(self):
        engine = HttpEngine(make_ct({"auth": {"type": "basic"}}))
        req = {"headers": {}}
        cred = Credential(raw=None, fields={"username": "user", "password": "pass"})
        engine._apply_basic(req, {}, cred)
        assert req["headers"]["Authorization"].startswith("Basic ")
        import base64

        decoded = base64.b64decode(
            req["headers"]["Authorization"].split(" ")[1]
        ).decode()
        assert decoded == "user:pass"

    def test_apply_custom_header(self):
        engine = HttpEngine(
            make_ct(
                {
                    "auth": {
                        "type": "custom_header",
                        "name": "X-Custom",
                        "template": "Token {{ cred.raw }}",
                    },
                }
            )
        )
        req = {"headers": {}}
        cred = make_cred(raw="secret-123")
        engine._apply_custom_header(req, engine.spec["auth"], cred)
        assert req["headers"]["X-Custom"] == "Token secret-123"

    def test_apply_fallback_bearer_when_no_auth_type(self):
        engine = HttpEngine(make_ct({}))
        req = {"headers": {}}
        cred = make_cred(raw="fallback-token")
        engine._apply_auth(req, cred, {})
        assert req["headers"]["Authorization"] == "Bearer fallback-token"


# ─── Session Capture ──────────────────────────────────────────────────────────


class TestHttpEngineSessionCapture:
    def test_session_capture_from_response_header(self):
        engine = HttpEngine(
            make_ct(
                {
                    "auth": {"type": "basic"},
                    "session": {
                        "type": "challenge_retry",
                        "trigger": {"http_status": 409},
                        "capture": {
                            "source": "response_header",
                            "name": "X-Session-Id",
                            "as": "session_id",
                        },
                        "apply": {
                            "target": "request_header",
                            "name": "X-Session-Id",
                            "from": "session_id",
                        },
                        "max_retries": 1,
                    },
                    "requests": {
                        "list_torrents": {
                            "method": "POST",
                            "path": "/rpc",
                            "body": {
                                "template": {"method": "torrent-get", "arguments": {}}
                            },
                            "response": {"success_when": "$.result == 'success'"},
                        }
                    },
                }
            )
        )
        engine._send = MagicMock(
            return_value=MagicMock(
                status=200,
                headers={"X-Session-Id": "captured-session-abc"},
                read=MagicMock(return_value=b'{"result": "success"}'),
            )
        )
        result = engine.refresh_session(make_cred(), "{}", None)
        assert result["session"]["session_id"] == "captured-session-abc"

    def test_apply_session_header(self):
        engine = HttpEngine(
            make_ct(
                {
                    "session": {
                        "apply": {
                            "target": "request_header",
                            "name": "X-Session-Id",
                            "from": "session_id",
                        },
                    },
                }
            )
        )
        req = {"headers": {}}
        engine._apply_session(req, {"session_id": "abc123"})
        assert req["headers"]["X-Session-Id"] == "abc123"

    def test_apply_session_no_session(self):
        engine = HttpEngine(make_ct({}))
        req = {"headers": {}}
        engine._apply_session(req, None)
        assert req["headers"] == {}


# ─── Session Challenge ─────────────────────────────────────────────────────────


class TestHttpEngineSessionChallenge:
    def test_is_session_challenge_true(self):
        engine = HttpEngine(
            make_ct(
                {
                    "session": {"trigger": {"http_status": 409}},
                }
            )
        )
        resp = MagicMock(status=409)
        assert engine._is_session_challenge(resp) is True

    def test_is_session_challenge_false(self):
        engine = HttpEngine(
            make_ct(
                {
                    "session": {"trigger": {"http_status": 409}},
                }
            )
        )
        resp = MagicMock(status=200)
        assert engine._is_session_challenge(resp) is False

    def test_send_wraps_http_409(self, monkeypatch):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "session": {"type": "challenge_retry", "trigger": {"http_status": 409}},
                    "requests": {
                        "list_torrents": {
                            "method": "POST",
                            "path": "/transmission/rpc",
                            "body": {"template": {"method": "torrent-get", "arguments": {}}},
                        }
                    },
                }
            )
        )

        def fake_safe_urlopen(req, timeout=30):
            raise urllib.error.HTTPError(
                req.full_url,
                409,
                "Conflict",
                {"X-Transmission-Session-Id": "abc123"},
                io.BytesIO(b'{"result":"session-id-required"}'),
            )

        monkeypatch.setattr("app.connectors.http_engine.safe_urlopen", fake_safe_urlopen)

        resp = engine._send(
            {
                "method": "POST",
                "url": "http://localhost:9091/transmission/rpc",
                "headers": {},
                "body": {"method": "torrent-get", "arguments": {}},
            },
            {},
        )

        assert resp.status == 409
        assert resp.headers.get("X-Transmission-Session-Id") == "abc123"
        assert b"session-id-required" in resp.read()


# ─── needs_session ─────────────────────────────────────────────────────────────


class TestNeedsSession:
    def test_needs_session_true_when_session_block(self):
        engine = HttpEngine(make_ct({"session": {"type": "challenge_retry"}}))
        assert engine.needs_session is True

    def test_needs_session_true_when_refresh_block(self):
        engine = HttpEngine(make_ct({"refresh": {}}))
        assert engine.needs_session is True

    def test_needs_session_false_when_neither(self):
        engine = HttpEngine(make_ct({}))
        assert engine.needs_session is False


# ─── Execute builds correct request ───────────────────────────────────────────


class TestHttpEngineExecute:
    def test_execute_builds_request_and_applies_auth(self):
        engine = HttpEngine(
            make_ct(
                {
                    "base_url": {"from": "config", "field": "base_url"},
                    "auth": {"type": "bearer"},
                    "requests": {
                        "list_torrents": {
                            "method": "POST",
                            "path": "/rpc",
                            "body": {
                                "template": {
                                    "method": "torrent-get",
                                    "arguments": {
                                        "ids": "{{ params.ids | default([], as=list) }}"
                                    },
                                }
                            },
                            "response": {
                                "success_when": "$.result == 'success'",
                                "extract": "$.arguments.torrents",
                            },
                        }
                    },
                }
            )
        )

        calls = []
        engine._send = MagicMock(
            side_effect=lambda req, config: calls.append(req)
            or MagicMock(
                status=200,
                read=MagicMock(
                    return_value=json.dumps(
                        {"result": "success", "arguments": {"torrents": [1, 2]}}
                    ).encode()
                ),
            )
        )
        engine._raise_on_errors = MagicMock()

        engine.execute(
            "list_torrents",
            {"ids": [1, 2]},
            make_cred(raw="bearer-token"),
            '{"base_url": "http://localhost:9091"}',
            session=None,
        )

        assert len(calls) == 1
        assert calls[0]["method"] == "POST"
        assert calls[0]["url"] == "http://localhost:9091/rpc"
        assert calls[0]["headers"]["Authorization"] == "Bearer bearer-token"
        assert calls[0]["body"]["method"] == "torrent-get"
        assert calls[0]["body"]["arguments"]["ids"] == [1, 2]

    def test_execute_unknown_action_returns_error(self):
        engine = HttpEngine(
            make_ct(
                {
                    "requests": {},
                }
            )
        )
        result = engine.execute("unknown_action", {}, make_cred(), "{}", None)
        assert result["success"] is False
        assert "No request defined" in result["error"]


# ─── Error Handling ────────────────────────────────────────────────────────────


class TestHttpEngineErrors:
    def test_raise_on_429(self):
        from app.connectors.errors import RateLimitedError

        engine = HttpEngine(make_ct({}))
        resp = MagicMock(status=429, headers={"Retry-After": "5"})
        with pytest.raises(RateLimitedError) as exc_info:
            engine._raise_on_errors(resp)
        assert exc_info.value.retry_after == 5.0

    def test_raise_on_session_challenge(self):
        from app.connectors.errors import SessionExpiredError

        engine = HttpEngine(
            make_ct(
                {
                    "session": {"trigger": {"http_status": 409}},
                }
            )
        )
        resp = MagicMock(status=409, headers={})
        with pytest.raises(SessionExpiredError):
            engine._raise_on_errors(resp)

    def test_raise_on_auth_expired(self):
        from app.connectors.errors import AuthExpiredError

        engine = HttpEngine(
            make_ct(
                {
                    "refresh": {"trigger": {"http_status": 401}},
                }
            )
        )
        resp = MagicMock(status=401, headers={})
        with pytest.raises(AuthExpiredError):
            engine._raise_on_errors(resp)


# ─── Response Extraction ──────────────────────────────────────────────────────


class TestHttpEngineExtract:
    def test_extract_simple(self):
        engine = HttpEngine(make_ct({}))
        resp = MagicMock(
            status=200,
            read=MagicMock(return_value=b'{"result": "success", "data": [1,2,3]}'),
        )
        result = engine._extract(resp, {"response": {"extract": "$.data"}}, {})
        assert result["body"] == [1, 2, 3]
        assert result["success"] is True

    def test_extract_nested_path(self):
        engine = HttpEngine(make_ct({}))
        resp = MagicMock(
            status=200,
            read=MagicMock(
                return_value=b'{"result": "success", "args": {"items": {"a": 1}}}'
            ),
        )
        result = engine._extract(resp, {"response": {"extract": "$.args.items"}}, {})
        assert result["body"] == {"a": 1}

    def test_extract_result_condition(self):
        engine = HttpEngine(make_ct({}))
        resp = MagicMock(
            status=200,
            read=MagicMock(
                return_value=b'{"result": "success", "arguments": {"torrents": []}}'
            ),
        )
        result = engine._extract(
            resp, {"response": {"success_when": "$.result == 'success'"}}, {}
        )
        assert result["success"] is True

    def test_extract_result_failure_condition(self):
        engine = HttpEngine(make_ct({}))
        resp = MagicMock(
            status=200,
            read=MagicMock(return_value=b'{"result": "error", "arguments": {}}'),
        )
        result = engine._extract(
            resp, {"response": {"success_when": "$.result == 'success'"}}, {}
        )
        assert result["success"] is False
