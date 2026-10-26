
import io
import json
import zipfile
import sqlite3
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


def test_replace_all_restore_round_trips_data(clean_db):
    """Back up, change everything, restore: the backup's state is what remains."""
    from app.config import settings
    from app.services import (
        backup_service,
        connector_service,
        credential_service,
        memory_service,
    )

    kept, _ = memory_service.write_memory(
        content="The ingest route is POST /api/webhook/health.",
        memory_class="fact",
        scope="workspace:proj",
    )
    credential = credential_service.create_credential(
        scope="workspace:proj", name="round_trip", value_plaintext="round-trip-secret"
    )
    connector_service.create_connector_type(
        connector_type_id="round-trip-mcp",
        display_name="Round Trip MCP",
        provider_type="mcp",
        auth_type="none",
        supported_actions=["tools/list"],
        endpoint_url="https://example.com/mcp",
        transport_type="streamable_http",
        capabilities_json='{"tools":true}',
        tool_snapshot_json='{"tools":[{"name":"tools/list"}]}',
    )
    connector_service.create_binding(
        connector_type_id="round-trip-mcp",
        name="round-trip-binding",
        scope="workspace:proj",
        enabled=True,
    )

    archive = backup_service.build_backup_zip(
        str(clean_db), str(settings.credential_key_path), "admin"
    )

    later, _ = memory_service.write_memory(
        content="Written after the backup and gone once it is restored.",
        memory_class="fact",
        scope="workspace:proj",
    )
    with sqlite3.connect(clean_db) as conn:
        conn.execute("DELETE FROM memory_records WHERE id = ?", (kept["id"],))
        conn.execute(
            "DELETE FROM connector_bindings WHERE connector_type_id = 'round-trip-mcp'"
        )
        conn.execute("DELETE FROM credentials WHERE id = ?", (credential["id"],))
        conn.commit()

    ok, msg, manifest = backup_service.restore_from_zip(
        archive, str(clean_db), str(settings.credential_key_path)
    )
    assert ok, msg
    assert manifest["exported_by"] == "admin"

    with sqlite3.connect(clean_db) as conn:
        memory_ids = {
            row[0] for row in conn.execute("SELECT id FROM memory_records").fetchall()
        }
        bindings = conn.execute(
            "SELECT COUNT(*) FROM connector_bindings WHERE connector_type_id = 'round-trip-mcp'"
        ).fetchone()[0]
    assert kept["id"] in memory_ids
    assert later["id"] not in memory_ids
    assert bindings == 1
    assert (
        credential_service.resolve_reference(credential["reference_name"])
        == "round-trip-secret"
    )
