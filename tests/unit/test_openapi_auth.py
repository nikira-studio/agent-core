import json

from app.connectors.openapi_executor import OpenApiExecutor
from app.connectors.generic_http import GenericHttpConnector
from app.services.openapi_service import import_spec, generate_tools
from app.security.url_validation import validate_public_url


def test_import_spec_preserves_auth_schemes():
    raw_spec = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "GitHub", "version": "1.0.0"},
            "servers": [{"url": "https://api.github.com"}],
            "paths": {},
            "components": {
                "securitySchemes": {
                    "github_pat": {
                        "type": "http",
                        "scheme": "bearer",
                    }
                }
            },
        }
    )

    result = import_spec(raw_spec, is_url=False)

    assert result["auth_type"] == "bearer"
    assert result["auth_schemes"][0]["name"] == "github_pat"
    assert result["auth_schemes"][0]["scheme"] == "bearer"
    operations_meta = json.loads(result["operations_json"])
    assert operations_meta["auth_schemes"][0]["auth_header"] == "Authorization"


def test_openapi_executor_applies_bearer_and_query_auth():
    executor = OpenApiExecutor()

    bearer_spec = {
        "auth_type": "bearer",
        "auth_schemes": [
            {
                "name": "github_pat",
                "type": "http",
                "scheme": "bearer",
                "auth_location": "header",
                "auth_header": "Authorization",
            }
        ],
    }
    headers = {}
    url = executor._apply_auth(
        bearer_spec,
        headers,
        "ghp_exampletoken",
        None,
        "https://api.github.com/user/repos",
    )

    assert url == "https://api.github.com/user/repos"
    assert headers["Authorization"] == "Bearer ghp_exampletoken"

    query_spec = {
        "auth_type": "api_key",
        "auth_schemes": [
            {
                "name": "api_key",
                "type": "apiKey",
                "scheme": "apiKey",
                "auth_location": "query",
                "auth_header": "api_key",
                "query_param": "api_key",
            }
        ],
    }
    headers = {}
    url = executor._apply_auth(
        query_spec,
        headers,
        "secret-token",
        None,
        "https://example.test/repos",
    )

    assert url.endswith("api_key=secret-token")
    assert "Authorization" not in headers


def test_generate_tools_includes_auth_and_input_summary():
    operations_json = json.dumps(
        {
            "auth_schemes": [
                {
                    "name": "github_pat",
                    "type": "http",
                    "scheme": "bearer",
                    "auth_location": "header",
                    "auth_header": "Authorization",
                }
            ],
            "operations": [
                {
                    "operation_id": "repos_list",
                    "method": "GET",
                    "path": "/user/repos",
                    "summary": "List repos",
                    "description": "",
                    "parameters": [
                        {
                            "name": "per_page",
                            "in": "query",
                            "required": False,
                            "description": "Page size",
                            "schema": {"type": "integer"},
                        },
                        {
                            "name": "page",
                            "in": "query",
                            "required": False,
                            "description": "",
                            "schema": {"type": "integer"},
                        },
                    ],
                    "request_body": None,
                    "tags": [],
                }
            ],
        }
    )

    result = generate_tools(
        connector_type_id="github",
        operations_json=operations_json,
        limit=10,
    )

    assert result["total"] == 1
    tool = result["tools"][0]
    assert tool["auth_summary"] == "bearer via Authorization"
    assert "Auth: bearer via Authorization" in tool["description"]
    assert "optional: per_page, page" in tool["description"]


def test_import_spec_rejects_private_server_url():
    raw_spec = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Private API", "version": "1.0.0"},
            "servers": [{"url": "http://169.254.169.254"}],
            "paths": {},
        }
    )

    try:
        import_spec(raw_spec, is_url=False)
        assert False, "Expected import_spec to reject private server URL"
    except ValueError as e:
        assert "Blocked private network host" in str(e)


def test_generic_http_rejects_private_url():
    connector = GenericHttpConnector()
    result = connector._call(
        credential="token",
        config={},
        params={"url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert result["success"] is False
    assert "Blocked private network host" in result["error"]


def test_validate_public_url_rejects_dns_rebinding(monkeypatch):
    import socket

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [
            (socket.AF_INET, None, None, None, ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr("app.security.url_validation.socket.getaddrinfo", fake_getaddrinfo)

    try:
        validate_public_url("https://example.test")
        assert False, "Expected validate_public_url to reject private resolution"
    except ValueError as e:
        assert "Blocked private network host" in str(e)
