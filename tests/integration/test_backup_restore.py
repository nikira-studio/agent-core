
import io
import json
import zipfile
from io import BytesIO

from app.branding import APP_SLUG


def test_backup_restore_requires_admin(test_client, agent_token):
    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code == 401


def test_backup_restore_requires_file(test_client, admin_token):
    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code in (400, 422)


def test_backup_restore_accepts_encrypted_archive_and_key(
    test_client, admin_token, clean_db
):
    from app.services import backup_service
    from app.config import settings

    encrypted_buf, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db),
        str(settings.credential_key_path),
        "admin",
    )
    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={
            "backup": (f"{APP_SLUG}-backup.zip.enc", BytesIO(encrypted_buf.getvalue())),
        },
        data={"backup_key": backup_key.decode(), "mode": "replace_all"},
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["mode"] == "replace_all"


def test_backup_restore_accepts_legacy_manifest_key(
    test_client, admin_token, clean_db
):
    from cryptography.fernet import Fernet
    from app.services import backup_service
    from app.config import settings

    plain_buf = backup_service.build_backup_zip(
        str(clean_db),
        str(settings.credential_key_path),
        "admin",
    )

    rewritten = io.BytesIO()
    with zipfile.ZipFile(plain_buf, "r") as src, zipfile.ZipFile(
        rewritten, "w", zipfile.ZIP_DEFLATED
    ) as dst:
        for name in src.namelist():
            data = src.read(name)
            if name == "manifest.json":
                manifest = json.loads(data)
                manifest["agent_core_version"] = manifest.pop("app_version")
                data = json.dumps(manifest, indent=2).encode()
            dst.writestr(name, data)

    backup_key = Fernet.generate_key()
    encrypted = Fernet(backup_key).encrypt(rewritten.getvalue())

    r = test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={
            "backup": (f"{APP_SLUG}-legacy-backup.zip.enc", BytesIO(encrypted)),
        },
        data={"backup_key": backup_key.decode(), "mode": "replace_all"},
    )

    assert r.status_code == 200, r.text
    assert r.json()["data"]["manifest"]["app_version"] == "1.0.0"


def test_merge_restore_preserves_connector_tables(clean_db):
    import tempfile
    from pathlib import Path
    import sqlite3
    from cryptography.fernet import Fernet

    from app.services import backup_service, connector_service
    from app.config import settings

    settings.credential_key_path.write_bytes(Fernet.generate_key())

    connector_service.create_connector_type(
        connector_type_id="merge-mcp",
        display_name="Merge MCP",
        provider_type="mcp",
        auth_type="none",
        supported_actions=["tools/list"],
        endpoint_url="https://example.com/mcp",
        transport_type="streamable_http",
        capabilities_json='{"tools":true}',
        tool_snapshot_json='{"tools":[{"name":"tools/list"}]}',
    )
    connector_service.create_binding(
        connector_type_id="merge-mcp",
        name="merge-binding",
        scope="workspace:test",
        enabled=True,
    )

    plain_buf = backup_service.build_backup_zip(
        str(clean_db),
        str(settings.credential_key_path),
        "admin",
    )

    tmpdir = Path(tempfile.mkdtemp())
    backup_zip = tmpdir / "backup.zip"
    backup_zip.write_bytes(plain_buf.getvalue())

    ok, msg, manifest = backup_service.merge_restore_from_zip(
        BytesIO(backup_zip.read_bytes()),
        str(clean_db),
        str(settings.credential_key_path),
    )
    assert ok, msg
    assert "connector_types" in manifest["merge"]["inserted_counts"]
    assert "connector_bindings" in manifest["merge"]["inserted_counts"]
    assert "connector_executions" in manifest["merge"]["inserted_counts"]

    with sqlite3.connect(clean_db) as conn:
        conn.row_factory = sqlite3.Row
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM connector_types WHERE id = 'merge-mcp'"
        ).fetchone()["c"] == 1
        assert conn.execute(
            "SELECT COUNT(*) AS c FROM connector_bindings WHERE connector_type_id = 'merge-mcp'"
        ).fetchone()["c"] == 1
