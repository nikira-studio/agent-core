import hashlib
import json
import sqlite3
import zipfile
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from cryptography.fernet import Fernet
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
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    tools = r.json()["data"]["mcp_tools"]
    tool_names = {t["name"] for t in tools}
    expected = {"memory_search", "memory_get", "memory_write", "memory_retract",
                "vault_get", "vault_list", "activity_update", "activity_get", "get_briefing"}
    assert expected.issubset(tool_names)


def test_spec_backup_restore_documents_merge_mode(test_client, admin_token):
    r = test_client.get(
        "/spec",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200
    br = r.json()["data"]["backup_restore"]
    assert "replace_all" in br
    assert "merge" in br["restore_modes"]
    assert "conflict_behavior" in br
    assert "existing record wins" in br["conflict_behavior"]
    assert "re-encrypts imported vault entries" in br["vault_key_handling"]


def test_prd_handoff_endpoint_rejects_unreadable_user_scope(test_client, admin_token):
    from app.services import agent_service
    from app.database import get_db

    with get_db() as conn:
        conn.execute(
            "INSERT INTO users (id, email, password_hash, display_name, role) VALUES (?, ?, ?, ?, ?)",
            ("victim", "victim@test.local", "unused", "Victim", "user"),
        )
        conn.commit()

    from_agent, from_key = agent_service.create_agent(
        agent_id="fromagent",
        display_name="From Agent",
        owner_user_id="admin",
        read_scopes=["agent:fromagent", "user:admin"],
        write_scopes=["agent:fromagent"],
    )
    agent_service.create_agent(
        agent_id="toagent",
        display_name="To Agent",
        owner_user_id="admin",
        read_scopes=["agent:toagent", "user:admin"],
        write_scopes=["agent:toagent"],
    )

    r = test_client.post(
        "/api/briefings/handoff/prd",
        headers={"Authorization": f"Bearer {from_key}"},
        json={"from_agent_id": "fromagent", "to_agent_id": "toagent", "user_id": "victim"},
    )
    assert r.status_code == 403


def test_prd_handoff_returns_prd_shape(test_client, admin_token):
    from app.services import agent_service, activity_service, memory_service

    agent_service.create_agent(
        agent_id="agent1",
        display_name="Agent One",
        owner_user_id="admin",
        read_scopes=["agent:agent1", "user:admin"],
        write_scopes=["agent:agent1"],
    )
    agent_service.create_agent(
        agent_id="agent2",
        display_name="Agent Two",
        owner_user_id="admin",
        read_scopes=["agent:agent2", "user:admin"],
        write_scopes=["agent:agent2"],
    )
    activity_service.create_activity(
        agent_id="agent1",
        user_id="admin",
        task_description="Test task",
        memory_scope="agent:agent1",
    )
    memory_service.write_memory(
        content="Use deterministic restore validation",
        memory_class="decision",
        scope="agent:agent1",
    )
    memory_service.write_memory(
        content="Admin context for handoff",
        memory_class="fact",
        scope="user:admin",
    )

    with patch("app.services.briefing_service.activity_service.get_active_activity_for_agent") as mock_active:
        mock_active.return_value = {
            "id": "activity-123",
            "task_description": "Test task",
            "started_at": "2026-04-27T08:00:00Z",
        }
        r = test_client.post(
            "/api/briefings/handoff/prd",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"from_agent_id": "agent1", "to_agent_id": "agent2", "user_id": "admin"},
        )
    assert r.status_code in (200, 201)
    data = r.json()["data"]["briefing"]
    assert data["from_agent"] == "agent1"
    assert data["to_agent"] == "agent2"
    assert data["user"] == "admin"
    assert "active_task" in data
    assert "recent_completed" in data
    assert "key_decisions" in data
    assert "active_context" in data


def test_backup_restore_accepts_merge_mode(test_client, admin_token):
    with patch("app.services.backup_service.merge_restore_from_zip") as mock_merge:
        mock_merge.return_value = (True, "", {"exported_by": "admin", "exported_at": "2026-04-27T00:00:00Z", "agent_core_version": "1.0.0"})
        r = test_client.post(
            "/api/backup/restore",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"mode": "merge"},
        )
        assert r.status_code in (200, 400, 415)


def _build_test_backup(tmp_path: Path, backup_key: bytes, *, tamper_manifest: bool = False) -> BytesIO:
    backup_db = tmp_path / "backup.db"
    con = sqlite3.connect(backup_db)
    con.executescript(SCHEMA_SQL)
    encrypted = Fernet(backup_key).encrypt(b"backup-secret").decode()
    con.execute(
        """
        INSERT INTO memory_records
        (id, content, memory_class, scope, confidence, importance, source_kind, event_time, created_at, record_status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("backup-memory", "Backup memory content", "fact", "user:admin", 0.9, 0.8, "operator_authored", "2026-04-30T00:00:00", "2026-04-30T00:00:00", "active"),
    )
    con.execute(
        """
        INSERT INTO vault_entries
        (id, scope, name, label, value_encrypted, value_type, reference_name, created_by)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("backup-vault", "user:admin", "backup_token", "Backup Token", encrypted, "api", "AC_SECRET_BACKUP_TOKEN_ABCD", "admin"),
    )
    con.commit()
    con.close()

    key_path = tmp_path / "backup-vault.key"
    key_path.write_bytes(backup_key)
    files = {
        "agent-core.db": backup_db.read_bytes(),
        "vault.key": key_path.read_bytes(),
    }
    manifest_files = {
        name: hashlib.sha256(data).hexdigest()
        for name, data in files.items()
    }
    if tamper_manifest:
        manifest_files["vault.key"] = "0" * 64
    manifest = {
        "agent_core_version": "1.0.0",
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


def test_merge_restore_imports_missing_rows_and_preserves_current_vault_key(clean_db, tmp_path):
    from app.config import settings
    from app.database import get_db
    from app.security import encryption
    from app.services import backup_service, vault_service

    encryption._fernet = None
    encryption.encrypt_value("current-secret")
    current_key = settings.vault_key_path.read_bytes()
    backup_key = Fernet.generate_key()
    assert backup_key != current_key

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        _build_test_backup(tmp_path, backup_key),
        str(clean_db),
        str(settings.vault_key_path),
    )
    assert ok, msg
    assert settings.vault_key_path.read_bytes() == current_key
    assert manifest["merge"]["inserted_counts"]["memory_records"] == 1
    assert manifest["merge"]["inserted_counts"]["vault_entries"] == 1

    with get_db() as conn:
        memory = conn.execute("SELECT content FROM memory_records WHERE id = ?", ("backup-memory",)).fetchone()
        vault = conn.execute("SELECT reference_name FROM vault_entries WHERE id = ?", ("backup-vault",)).fetchone()
    assert memory["content"] == "Backup memory content"
    assert vault["reference_name"] == "AC_SECRET_BACKUP_TOKEN_ABCD"
    assert vault_service.resolve_reference("AC_SECRET_BACKUP_TOKEN_ABCD") == "backup-secret"


def test_merge_restore_rejects_checksum_mismatch(clean_db, tmp_path):
    from app.config import settings
    from app.security import encryption
    from app.services import backup_service

    encryption._fernet = None
    encryption.encrypt_value("current-secret")
    ok, msg, manifest = backup_service.merge_restore_from_zip(
        _build_test_backup(tmp_path, Fernet.generate_key(), tamper_manifest=True),
        str(clean_db),
        str(settings.vault_key_path),
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
    assert "solo_mode" in ff
