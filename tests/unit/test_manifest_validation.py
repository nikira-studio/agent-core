"""Unit tests for manifest loading and validation."""

import json
import tempfile
from pathlib import Path


from app.connectors.manifest import (
    ADAPTER_MANIFEST_SCHEMA,
    Manifest,
    ManifestValidationError,
    load_and_validate,
)
import jsonschema


# ─── load_and_validate ────────────────────────────────────────────────────────


class TestLoadAndValidate:
    def test_load_valid_transmission_manifest(self):
        valid = {
            "spec_version": "1.0",
            "id": "test_connector",
            "version": "1.0.0",
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(valid, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert err is None
            assert m is not None
            assert m.id == "test_connector"
            assert m.spec_version == "1.0"
        finally:
            path.unlink()

    def test_load_valid_full_manifest(self):
        data = {
            "spec_version": "1.0",
            "id": "full_test",
            "display_name": "Full Test",
            "version": "1.0.0",
            "description": "A test manifest",
            "credential_schema": {
                "fields": [
                    {
                        "name": "username",
                        "type": "string",
                        "secret": False,
                        "required": True,
                    },
                    {
                        "name": "password",
                        "type": "string",
                        "secret": True,
                        "required": True,
                    },
                ]
            },
            "requires": {"config": ["base_url"], "env": [], "bins": []},
            "actions": [
                {
                    "name": "list_items",
                    "description": "List items",
                    "side_effect": "read",
                    "input_schema": {
                        "type": "object",
                        "properties": {"ids": {"type": "array"}},
                    },
                },
                {
                    "name": "delete_item",
                    "description": "Delete item",
                    "side_effect": "destructive",
                    "input_schema": {
                        "type": "object",
                        "properties": {"id": {"type": "string"}},
                    },
                },
            ],
            "backend": {
                "type": "http",
                "base_url": {"from": "config", "field": "base_url"},
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
                    "list_items": {
                        "method": "POST",
                        "path": "/items",
                        "body": {"template": {"method": "list"}},
                        "response": {"success_when": "$.ok == true"},
                    },
                    "delete_item": {
                        "method": "POST",
                        "path": "/items",
                        "body": {
                            "template": {"method": "delete", "id": "{{ params.id }}"}
                        },
                        "response": {"success_when": "$.ok == true"},
                    },
                },
            },
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert err is None
            assert m.id == "full_test"
            assert m.display_name == "Full Test"
            assert len(m.actions) == 2
            assert m.backend["type"] == "http"
            assert m.backend["auth"]["type"] == "basic"
        finally:
            path.unlink()

    def test_missing_spec_version(self):
        data = {
            "id": "bad1",
            "version": "1.0.0",
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_missing_id(self):
        data = {
            "spec_version": "1.0",
            "version": "1.0.0",
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_missing_backend(self):
        data = {
            "spec_version": "1.0",
            "id": "bad2",
            "version": "1.0.0",
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_invalid_spec_version(self):
        data = {
            "spec_version": "99.0",
            "id": "bad3",
            "version": "1.0.0",
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_invalid_version_format(self):
        data = {
            "spec_version": "1.0",
            "id": "bad4",
            "version": "not-semver",
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_invalid_id_pattern(self):
        data = {
            "spec_version": "1.0",
            "id": "Bad-ID!",  # has uppercase and special chars
            "version": "1.0.0",
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_invalid_json(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            f.write("{bad json")
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
            assert "Invalid JSON" in str(err)
        finally:
            path.unlink()

    def test_non_object_manifest(self):
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(["not", "an", "object"], f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
            assert "object" in str(err)
        finally:
            path.unlink()

    def test_invalid_backend_type(self):
        data = {
            "spec_version": "1.0",
            "id": "bad_type",
            "version": "1.0.0",
            "backend": {"type": "ftp", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()

    def test_invalid_side_effect(self):
        data = {
            "spec_version": "1.0",
            "id": "bad_side",
            "version": "1.0.0",
            "actions": [{"name": "act1", "side_effect": "explode"}],
            "backend": {"type": "http", "requests": {}},
        }
        with tempfile.NamedTemporaryFile(suffix=".json", mode="w", delete=False) as f:
            json.dump(data, f)
            path = Path(f.name)
        try:
            m, err = load_and_validate(path)
            assert m is None
            assert err is not None
        finally:
            path.unlink()


# ─── Manifest.to_connector_type_row ──────────────────────────────────────────


class TestManifestToRow:
    def test_to_connector_type_row_basic(self):
        data = {
            "spec_version": "1.0",
            "id": "my_connector",
            "display_name": "My Connector",
            "version": "2.1.0",
            "description": "Does things",
            "credential_schema": {
                "fields": [
                    {
                        "name": "api_key",
                        "type": "string",
                        "secret": True,
                        "required": True,
                    },
                ]
            },
            "actions": [
                {"name": "list", "description": "List items", "side_effect": "read"},
                {
                    "name": "delete",
                    "description": "Delete item",
                    "side_effect": "destructive",
                },
            ],
            "backend": {
                "type": "http",
                "requests": {"list": {"method": "GET", "path": "/items"}},
            },
        }
        m = Manifest(data)
        row = m.to_connector_type_row()
        assert row["id"] == "my_connector"
        assert row["display_name"] == "My Connector"
        assert row["description"] == "Does things"
        assert row["version"] == "2.1.0"
        assert row["backend_type"] == "http"
        actions = json.loads(row["supported_actions_json"])
        assert len(actions) == 2
        assert actions[0]["name"] == "list"
        assert actions[1]["side_effect"] == "destructive"

    def test_to_connector_type_row_minimal(self):
        data = {
            "spec_version": "1.0",
            "id": "min_conn",
            "version": "0.1.0",
            "backend": {"type": "http"},
        }
        m = Manifest(data)
        row = m.to_connector_type_row()
        assert row["id"] == "min_conn"
        assert row["display_name"] == "min_conn"
        assert row["backend_type"] == "http"


# ─── ManifestValidationError ──────────────────────────────────────────────────


class TestManifestValidationError:
    def test_repr_with_path(self):
        err = ManifestValidationError("field required", "backend.type")
        assert "backend.type" in repr(err)
        assert "field required" in repr(err)

    def test_repr_without_path(self):
        err = ManifestValidationError("just a message")
        assert "just a message" in repr(err)


# ─── Schema validation ─────────────────────────────────────────────────────────


class TestSchema:
    def test_schema_allows_all_side_effects(self):
        for se in ("none", "read", "write", "destructive"):
            data = {
                "spec_version": "1.0",
                "id": f"test_{se}",
                "version": "1.0.0",
                "actions": [{"name": "act", "side_effect": se}],
                "backend": {"type": "http", "requests": {}},
            }
            jsonschema.validate(data, ADAPTER_MANIFEST_SCHEMA)

    def test_schema_allows_http_backend(self):
        data = {
            "spec_version": "1.0",
            "id": "http_only",
            "version": "1.0.0",
            "backend": {"type": "http", "requests": {}},
        }
        jsonschema.validate(data, ADAPTER_MANIFEST_SCHEMA)

    def test_schema_allows_mcp_backend(self):
        data = {
            "spec_version": "1.0",
            "id": "mcp_conn",
            "version": "1.0.0",
            "backend": {"type": "mcp"},
        }
        jsonschema.validate(data, ADAPTER_MANIFEST_SCHEMA)

    def test_schema_allows_cli_backend(self):
        data = {
            "spec_version": "1.0",
            "id": "cli_conn",
            "version": "1.0.0",
            "backend": {"type": "cli"},
        }
        jsonschema.validate(data, ADAPTER_MANIFEST_SCHEMA)
