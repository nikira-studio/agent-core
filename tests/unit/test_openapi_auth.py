import json

from app.connectors.openapi_executor import OpenApiExecutor
from app.connectors.generic_http import GenericHttpConnector
from app.services.openapi_service import import_spec, generate_tools
from app.routes.connectors import _group_directory_entries
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


def test_generate_tools_preserves_operation_ids():
    operations_json = json.dumps(
        {
            "auth_schemes": [],
            "operations": [
                {
                    "operation_id": "items_list",
                    "method": "GET",
                    "path": "/items",
                    "summary": "List items",
                    "description": "",
                    "parameters": [],
                    "request_body": None,
                    "tags": ["items"],
                },
                {
                    "operation_id": "items_get",
                    "method": "GET",
                    "path": "/items/{item_id}",
                    "summary": "Get item",
                    "description": "",
                    "parameters": [],
                    "request_body": None,
                    "tags": ["items"],
                },
            ],
        }
    )

    result = generate_tools(
        connector_type_id="example_api",
        operations_json=operations_json,
        limit=10,
    )

    assert [tool["action"] for tool in result["tools"]] == [
        "items_list",
        "items_get",
    ]
    assert [tool["name"] for tool in result["tools"]] == [
        "example_api_items_list",
        "example_api_items_get",
    ]


def test_group_directory_entries_preserves_variants():
    raw_entries = [
        {
            "id": "github.com:api.github.com",
            "display_name": "GitHub v3 REST API",
            "provider": "github.com",
            "version": "1.1.4",
            "spec_url": "https://api.apis.guru/v2/specs/github.com/api.github.com/1.1.4/openapi.json",
        },
        {
            "id": "github.com",
            "display_name": "GitHub v3 REST API",
            "provider": "github.com",
            "version": "1.1.4",
            "spec_url": "https://api.apis.guru/v2/specs/github.com/1.1.4/openapi.json",
        },
        {
            "id": "github.com:ghec",
            "display_name": "GitHub v3 REST API",
            "provider": "github.com",
            "version": "1.1.4",
            "spec_url": "https://api.apis.guru/v2/specs/github.com/ghec/1.1.4/openapi.json",
        },
    ]

    grouped = _group_directory_entries(raw_entries)

    assert len(grouped) == 1
    group = grouped[0]
    assert group["variant_count"] == 3
    assert [v["id"] for v in group["variants"]] == [
        "github.com",
        "github.com:api.github.com",
        "github.com:ghec",
    ]


def test_get_directory_marks_grouped_variants_installed(monkeypatch):
    import asyncio
    from types import SimpleNamespace

    from app.routes import connectors
    from app.services import connector_service

    monkeypatch.setattr(
        connectors,
        "_fetch_directory",
        lambda: [
            {
                "id": "github.com",
                "display_name": "GitHub v3 REST API",
                "provider": "github.com",
                "version": "1.1.4",
                "spec_url": "https://api.apis.guru/v2/specs/github.com/1.1.4/openapi.json",
                "variant_count": 2,
                "variants": [
                    {
                        "id": "github.com",
                        "display_name": "GitHub v3 REST API",
                        "provider": "github.com",
                        "version": "1.1.4",
                        "spec_url": "https://api.apis.guru/v2/specs/github.com/1.1.4/openapi.json",
                    },
                    {
                        "id": "github.com:ghec",
                        "display_name": "GitHub v3 REST API",
                        "provider": "github.com",
                        "version": "1.1.4",
                        "spec_url": "https://api.apis.guru/v2/specs/github.com/ghec/1.1.4/openapi.json",
                    },
                ],
            }
        ],
    )
    monkeypatch.setattr(
        connector_service,
        "list_connector_types",
        lambda include_inactive=False: [{"id": "github.com:ghec"}],
    )

    from app.routes.connectors import get_directory

    response = asyncio.run(get_directory(ctx=SimpleNamespace(is_admin=True)))
    result = json.loads(response.body.decode())
    group = result["data"]["entries"][0]
    assert group["installed"] is False
    assert group["variants"][0]["installed"] is False
    assert group["variants"][1]["installed"] is True


def test_import_spec_allows_private_server_url_by_default():
    raw_spec = json.dumps(
        {
            "openapi": "3.0.0",
            "info": {"title": "Private API", "version": "1.0.0"},
            "servers": [{"url": "http://169.254.169.254"}],
            "paths": {},
        }
    )

    result = import_spec(raw_spec, is_url=False)
    assert result["connector_type_id"] == "private-api"
    assert result["auth_type"] == "none"


def test_generic_http_allows_private_url_by_default(monkeypatch):
    connector = GenericHttpConnector()
    captured = {}

    class FakeResponse:
        status = 200

        def __init__(self):
            self.headers = {}

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps({"ok": True}).encode()

    def fake_safe_urlopen(req, timeout=30):
        captured["url"] = req.full_url
        return FakeResponse()

    monkeypatch.setattr("app.connectors.generic_http.safe_urlopen", fake_safe_urlopen)
    result = connector._call(
        credential="token",
        config={},
        params={"url": "http://169.254.169.254/latest/meta-data/"},
    )
    assert result["success"] is True
    assert captured["url"] == "http://169.254.169.254/latest/meta-data/"


def test_validate_public_url_blocks_dns_rebinding_when_disabled(monkeypatch):
    import socket
    from app.config import settings

    def fake_getaddrinfo(host, *_args, **_kwargs):
        return [
            (socket.AF_INET, None, None, None, ("10.0.0.5", 0)),
        ]

    monkeypatch.setattr(settings, "BLOCK_INTERNAL_HOSTS", True, raising=False)
    monkeypatch.setattr("app.security.url_validation.socket.getaddrinfo", fake_getaddrinfo)

    try:
        validate_public_url("https://example.test")
        assert False, "Expected validate_public_url to reject private resolution when blocked"
    except ValueError as e:
        assert "Blocked private network host" in str(e)
