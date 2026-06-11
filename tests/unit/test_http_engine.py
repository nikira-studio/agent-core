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


OAUTH_BACKEND = {
    "auth": {
        "type": "oauth2",
        "apply": {
            "target": "request_header",
            "name": "Authorization",
            "template": "Bearer {{ cred.access_token }}",
        },
    },
    "refresh": {"trigger": {"http_status": 401, "or_expired": "cred.expires_at"}},
    "requests": {},
}


class TestOAuth2RefreshApplication:
    """Regression: OAuth2 token refresh was fully broken once the first access
    token's clock time passed. Two bugs: (1) the refreshed session token was not
    applied (the stale credential token was rendered in instead); (2) a 2xx
    response was re-flagged as auth-expired by the cred.expires_at check."""

    def test_session_token_overrides_stale_credential(self):
        engine = HttpEngine(make_ct(OAUTH_BACKEND))
        req = {"headers": {}}
        cred = make_cred(fields={"access_token": "OLD"})
        engine._apply_oauth2(req, OAUTH_BACKEND["auth"], cred, {"access_token": "NEW"})
        assert req["headers"]["Authorization"] == "Bearer NEW"

    def test_credential_token_used_without_session(self):
        engine = HttpEngine(make_ct(OAUTH_BACKEND))
        req = {"headers": {}}
        cred = make_cred(fields={"access_token": "TOK"})
        engine._apply_oauth2(req, OAUTH_BACKEND["auth"], cred, None)
        assert req["headers"]["Authorization"] == "Bearer TOK"

    def test_successful_response_not_flagged_expired(self):
        engine = HttpEngine(make_ct(OAUTH_BACKEND))
        cred = make_cred(fields={"expires_at": "1"})  # far in the past
        assert engine._is_auth_expired(MagicMock(status=200), None, cred) is False
        # a real auth failure still triggers refresh
        assert engine._is_auth_expired(MagicMock(status=401), None, cred) is True


class TestRfc822Attachments:
    """Phase 2: the rfc822_base64url builder gains multipart attachment support."""

    @staticmethod
    def _decode(p):
        import base64
        from email import message_from_bytes
        from app.connectors.http_engine import _make_rfc822_base64url
        raw_b64 = _make_rfc822_base64url(p)
        # base64url, padding stripped — restore it before decoding
        pad = "=" * (-len(raw_b64) % 4)
        return message_from_bytes(base64.urlsafe_b64decode(raw_b64 + pad))

    def test_no_attachments_is_single_part(self):
        msg = self._decode({"to": ["a@b.com"], "subject": "Hi", "body": "hello"})
        assert not msg.is_multipart()
        assert msg["To"] == "a@b.com"
        assert msg["Subject"] == "Hi"
        assert msg.get_payload() == "hello"

    def test_single_part_bytes_unchanged(self):
        # Back-compat: byte-identical to the original hand-rolled single-part output.
        import base64
        from app.connectors.http_engine import _make_rfc822_base64url
        p = {"to": ["a@b.com"], "cc": ["c@d.com"], "subject": "S", "body": "B"}
        expected_raw = ("To: a@b.com\r\nCc: c@d.com\r\nSubject: S\r\n\r\nB").encode("utf-8")
        expected = base64.urlsafe_b64encode(expected_raw).rstrip(b"=").decode("utf-8")
        assert _make_rfc822_base64url(p) == expected

    def test_one_attachment(self):
        import base64
        content = b"PDF-BYTES-HERE"
        msg = self._decode({
            "to": ["a@b.com"], "subject": "doc", "body": "see attached",
            "attachments": [{
                "filename": "report.pdf",
                "content_base64": base64.b64encode(content).decode(),
                "mime_type": "application/pdf",
            }],
        })
        assert msg.is_multipart()
        assert msg.get_content_type() == "multipart/mixed"
        parts = msg.get_payload()
        assert parts[0].get_content_type() == "text/plain"
        assert parts[0].get_payload(decode=True) == b"see attached"
        att = parts[1]
        assert att.get_content_type() == "application/pdf"
        assert att.get_filename() == "report.pdf"
        assert "attachment" in att["Content-Disposition"]
        assert att.get_payload(decode=True) == content  # round-trips intact

    def test_multiple_attachments_and_mime_default(self):
        import base64
        msg = self._decode({
            "to": ["a@b.com"], "subject": "two", "body": "body",
            "attachments": [
                {"filename": "a.txt", "content_base64": base64.b64encode(b"aaa").decode(), "mime_type": "text/plain"},
                {"filename": "b.bin", "content_base64": base64.b64encode(b"bbb").decode()},  # no mime_type
            ],
        })
        parts = msg.get_payload()
        assert len(parts) == 3  # body + 2 attachments
        assert parts[1].get_filename() == "a.txt"
        assert parts[2].get_filename() == "b.bin"
        assert parts[2].get_content_type() == "application/octet-stream"  # default

    def test_invalid_base64_raises(self):
        from app.connectors.http_engine import _make_rfc822_base64url
        with pytest.raises(ValueError, match="invalid base64"):
            _make_rfc822_base64url({
                "to": ["a@b.com"], "subject": "x", "body": "y",
                "attachments": [{"filename": "bad", "content_base64": "!!!not base64!!!"}],
            })

    def test_size_cap_rejected(self, monkeypatch):
        import base64
        import app.connectors.http_engine as eng
        monkeypatch.setattr(eng, "_RFC822_MAX_BYTES", 100)
        big = base64.b64encode(b"x" * 500).decode()
        with pytest.raises(ValueError, match="35MB"):
            eng._make_rfc822_base64url({
                "to": ["a@b.com"], "subject": "big", "body": "b",
                "attachments": [{"filename": "big.bin", "content_base64": big}],
            })
