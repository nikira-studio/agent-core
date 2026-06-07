"""Adapter manifest schema and loader."""

import json
import logging
from pathlib import Path
from typing import Optional

import jsonschema

logger = logging.getLogger(__name__)

ADAPTER_MANIFEST_SCHEMA = {
    "type": "object",
    "required": ["spec_version", "id", "version", "backend"],
    "properties": {
        "spec_version": {"type": "string", "enum": ["1.0"]},
        "id": {"type": "string", "pattern": "^[a-z0-9_]+$"},
        "display_name": {"type": "string"},
        "version": {"type": "string", "pattern": r"^\d+\.\d+\.\d+$"},
        "description": {"type": "string"},
        "setup": {
            "type": "object",
            "properties": {
                "instructions": {"type": "string"},
                "documentation_url": {
                    "type": "string",
                    "pattern": "^https://",
                },
            },
        },
        "credential_schema": {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "required": ["name"],
                        "properties": {
                            "name": {"type": "string"},
                            "secret": {"type": "boolean"},
                            "required": {"type": "boolean"},
                        },
                    },
                }
            },
        },
        "requires": {
            "type": "object",
            "properties": {
                "config": {"type": "array", "items": {"type": "string"}},
                "env": {"type": "array", "items": {"type": "string"}},
                "bins": {"type": "array", "items": {"type": "string"}},
            },
        },
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["name"],
                "properties": {
                    "name": {"type": "string"},
                    "side_effect": {
                        "type": "string",
                        "enum": ["none", "read", "write", "destructive"],
                    },
                    "input_schema": {"type": "object"},
                    "description": {"type": "string"},
                },
            },
        },
        "backend": {
            "type": "object",
            "required": ["type"],
            "properties": {
                "type": {"type": "string", "enum": ["http", "mcp", "cli"]},
                "base_url": {
                    "oneOf": [
                        {
                            "type": "object",
                            "properties": {
                                "from": {"type": "string"},
                                "field": {"type": "string"},
                            },
                        },
                        {"type": "string"},
                    ]
                },
                "auth": {"type": "object"},
                "test_action": {"type": "string"},
                "session": {"type": "object"},
                "refresh": {"type": "object"},
                "requests": {"type": "object"},
                "bin": {"type": "string"},
                "timeout": {"type": "integer"},
                "env": {
                    "type": "object",
                    "additionalProperties": {"type": "string"},
                },
                "commands": {"type": "object"},
            },
        },
    },
}

ADAPTER_MANIFEST_URI = "https://schemas.agent-core.dev/adapter-manifest/v1.0.json"


def _make_resolver() -> jsonschema.RefResolver:
    schema_store = {
        "https://schemas.agent-core.dev/adapter-manifest/v1.0.json": ADAPTER_MANIFEST_SCHEMA
    }
    return jsonschema.RefResolver.from_schema(
        {"$ref": ADAPTER_MANIFEST_SCHEMA["$id"]}, store=schema_store
    )


class ManifestValidationError:
    def __init__(self, message: str, path: Optional[str] = None):
        self.message = message
        self.path = path

    def __repr__(self) -> str:
        if self.path:
            return (
                f"ManifestValidationError(path={self.path!r}, message={self.message!r})"
            )
        return f"ManifestValidationError(message={self.message!r})"


class Manifest:
    """Validated adapter manifest envelope."""

    def __init__(self, data: dict):
        self.spec_version: str = data["spec_version"]
        self.id: str = data["id"]
        self.display_name: Optional[str] = data.get("display_name")
        self.version: str = data["version"]
        self.description: Optional[str] = data.get("description")
        self.setup: Optional[dict] = data.get("setup")
        self.credential_schema: Optional[dict] = data.get("credential_schema")
        self.requires: Optional[dict] = data.get("requires")
        self.actions: list[dict] = data.get("actions", [])
        self.backend: dict = data["backend"]

    def to_connector_type_row(self) -> dict:
        credential_fields = []
        if isinstance(self.credential_schema, dict):
            for field in self.credential_schema.get("fields", []) or []:
                name = field.get("name")
                if name and field.get("required", True):
                    credential_fields.append(name)
        backend_auth = self.backend.get("auth") if isinstance(self.backend, dict) else None
        if isinstance(backend_auth, dict):
            auth_type = backend_auth.get("type") or "none"
        elif credential_fields:
            auth_type = "api_key"
        else:
            auth_type = "none"
        return {
            "id": self.id,
            "display_name": self.display_name or self.id,
            "description": self.description or "",
            "version": self.version,
            "provider_type": "builtin",
            "auth_type": auth_type,
            "required_credential_fields_json": json.dumps(credential_fields),
            "supported_actions_json": json.dumps(self.actions),
            "backend_type": self.backend.get("type"),
            "backend_json": json.dumps(self.backend),
        }


def load_and_validate(
    path: Path,
) -> tuple[Optional[Manifest], Optional[ManifestValidationError]]:
    try:
        text = path.read_text()
    except OSError as e:
        return None, ManifestValidationError(f"Cannot read file: {e}")

    try:
        data = json.loads(text)
    except json.JSONDecodeError as e:
        return None, ManifestValidationError(f"Invalid JSON: {e}", path=str(path))

    if not isinstance(data, dict):
        return None, ManifestValidationError("Manifest must be a JSON object")

    try:
        jsonschema.validate(data, ADAPTER_MANIFEST_SCHEMA)
    except jsonschema.ValidationError as e:
        path_str = ".".join(str(p) for p in e.path) if e.path else None
        return None, ManifestValidationError(e.message, path_str)

    return Manifest(data), None
