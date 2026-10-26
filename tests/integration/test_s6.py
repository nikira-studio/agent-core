import hashlib
import json
import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path

from cryptography.fernet import Fernet
from app.branding import CREDENTIAL_PREFIX, DB_FILENAME, MANIFEST_VERSION_KEY
from app.schema import SCHEMA_SQL


def test_spec_endpoint_requires_auth(test_client):
    r = test_client.get("/spec")
    assert r.status_code in (401, 403)


def test_spec_endpoint_returns_full_spec(test_client, admin_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["version"] == "1.0.0"
    assert data["mcp_endpoint"] == "/mcp"
    assert data["rest_api_prefix"] == "/api"
    assert "scope_model" in data
    assert "rest_endpoints" in data
    assert "mcp_tools" in data
    assert "broker_behavior" in data
    assert "rate_limits" in data
    assert "backup_restore" in data
    assert "feature_flags" in data


def test_spec_endpoint_allows_agent_discovery(test_client, agent_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 200
    data = r.json()["data"]
    assert data["mcp_endpoint"] == "/mcp"
    assert "mcp_tools" in data


def test_spec_includes_all_mcp_tools(test_client, admin_token):
    """The /spec capability listing must exactly match the MCP manifest.

    Exact equality, not subset: a subset check let three tools
    (activity_pickup, connectors_summary, result_fetch) ship in the MCP
    manifest without ever appearing in /spec, so the "full capability spec"
    silently under-reported capabilities."""
    from app.routes.mcp import MANIFEST

    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    spec_tool_names = {t["name"] for t in r.json()["data"]["mcp_tools"]}
    manifest_tool_names = {t["name"] for t in MANIFEST["tools"]}
    assert spec_tool_names == manifest_tool_names, (
        f"missing from /spec: {manifest_tool_names - spec_tool_names}; "
        f"stale in /spec: {spec_tool_names - manifest_tool_names}"
    )


def test_memory_pinned_description_matches_workspace_sync_guidance(
    test_client, admin_token
):
    from app.routes.mcp import MANIFEST

    response = test_client.get(
        "/spec", headers={"Authorization": f"Bearer {admin_token}"}
    )
    assert response.status_code == 200

    manifest_tool = next(
        tool for tool in MANIFEST["tools"] if tool["name"] == "memory_pinned"
    )
    spec_tool = next(
        tool
        for tool in response.json()["data"]["mcp_tools"]
        if tool["name"] == "memory_pinned"
    )
    assert spec_tool["description"] == manifest_tool["description"]
    assert "workspace_sync already returns pinned records" in manifest_tool["description"]
    assert "other authorized scopes" in manifest_tool["description"]


def test_spec_includes_connector_endpoints(test_client, admin_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    endpoints = r.json()["data"]["rest_endpoints"]
    prefixes = {e["prefix"] for e in endpoints}
    assert "/api/connector-types" in prefixes
    assert "/api/connector-bindings" in prefixes
    assert "/api/connector-types/import-mcp" in prefixes
    assert "/api/connector-types/{connector_type_id}/refresh" in prefixes
    assert "/api/connector-bindings/{binding_id}/run" in prefixes


def test_spec_backup_restore_documents_replace_all_only(test_client, admin_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    br = r.json()["data"]["backup_restore"]
    assert br["restore_modes"] == ["replace_all"]
    assert "replace_all" in br
    assert "merge" not in br


def test_backup_restore_rejects_merge_mode(test_client, admin_token):
    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"mode": "merge"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INVALID_MODE"


def _build_test_backup(
    tmp_path: Path, backup_key: bytes, *, tamper_manifest: bool = False
) -> BytesIO:
    backup_db = tmp_path / "backup.db"
    con = sqlite3.connect(backup_db)
    con.executescript(SCHEMA_SQL)
    encrypted = Fernet(backup_key).encrypt(b"backup-secret").decode()
    con.execute(
        """
        INSERT INTO memory_records
        (id, content, memory_class, scope, confidence, importance, source_kind, created_at, record_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "backup-memory",
            "Backup memory content",
            "fact",
            "user:admin",
            0.9,
            0.8,
            "operator_authored",
            "2026-04-30T00:00:00",
            "active",
        ),
    )
    con.execute(
        """
        INSERT INTO credentials
        (id, scope, name, label, value_encrypted, reference_name, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "backup-credential",
            "user:admin",
            "backup_token",
            "Backup Token",
            encrypted,
            f"{CREDENTIAL_PREFIX}BACKUP_TOKEN_ABCD",
            "admin",
        ),
    )
    con.commit()
    con.close()

    key_path = tmp_path / "backup-credential.key"
    key_path.write_bytes(backup_key)
    files = {
        DB_FILENAME: backup_db.read_bytes(),
        "credential.key": key_path.read_bytes(),
    }
    manifest_files = {
        name: hashlib.sha256(data).hexdigest() for name, data in files.items()
    }
    if tamper_manifest:
        manifest_files["credential.key"] = "0" * 64
    manifest = {
        MANIFEST_VERSION_KEY: "1.0.0",
        "exported_at": "2026-04-30T00:00:00Z",
        "exported_by": "admin",
        "files": manifest_files,
    }
    buf = BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in files.items():
            zf.writestr(name, data)
        zf.writestr("manifest.json", json.dumps(manifest))
    buf.seek(0)
    return buf


def test_restore_rejects_checksum_mismatch(clean_db, tmp_path):
    from app.config import settings
    from app.security import encryption
    from app.services import backup_service

    encryption._fernet = None
    encryption.encrypt_value("current-secret")
    ok, msg, manifest = backup_service.restore_from_zip(
        _build_test_backup(tmp_path, Fernet.generate_key(), tamper_manifest=True),
        str(clean_db),
        str(settings.credential_key_path),
    )
    assert not ok
    assert "checksum mismatch" in msg


def test_backup_restore_rejects_invalid_mode(test_client, admin_token):
    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
        data={"mode": "invalid_mode"},
    )
    assert r.status_code == 400


def test_rate_limits_in_spec(test_client, admin_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    rl = r.json()["data"]["rate_limits"]
    assert "memory_write_agent" in rl
    assert "memory_search_agent" in rl
    assert rl["memory_write_agent"]["limit"] == 60


def test_feature_flags_includes_semantic_search(test_client, admin_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    ff = r.json()["data"]["feature_flags"]
    assert "semantic_search" in ff
    assert "solo_mode" not in ff
