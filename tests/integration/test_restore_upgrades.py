"""A backup from before the agent kind existed is brought up to date, under the access check, before it goes live."""

import json
import sqlite3
from io import BytesIO

from app.branding import APP_SLUG
from app.database import get_db


def _restore(test_client, admin_token, clean_db):
    from app.config import settings
    from app.services import backup_service

    encrypted_buf, backup_key = backup_service.build_encrypted_backup_package(
        str(clean_db), str(settings.credential_key_path), "admin"
    )
    return test_client.post(
        "/api/backup/restore",
        headers={"Authorization": f"Bearer {admin_token}"},
        files={"backup": (f"{APP_SLUG}-backup.zip.enc", BytesIO(encrypted_buf.getvalue()))},
        data={"backup_key": backup_key.decode(), "mode": "replace_all"},
    )


def test_an_older_backup_gets_kinds_before_it_goes_live(test_client, admin_token, clean_db):
    with get_db() as conn:
        conn.execute(
            "INSERT INTO agents (id, display_name, owner_user_id, read_scopes_json, write_scopes_json, "
            "capabilities_json, api_key_hash) VALUES ('worker-a', 'Worker A', 'admin', '[\"agent:worker-a\"]', "
            "'[\"agent:worker-a\"]', ?, 'h1')",
            (json.dumps(["credentials_resolve"]),),
        )
        conn.execute("DROP INDEX IF EXISTS idx_agents_kind")
        conn.execute("ALTER TABLE agents DROP COLUMN kind")
        conn.execute("DELETE FROM schema_migrations WHERE revision = 30")
        conn.commit()

    response = _restore(test_client, admin_token, clean_db)
    assert response.status_code == 200, response.text

    from app.config import settings

    # The restore writes the file named by the settings (the live database in production).
    restored = sqlite3.connect(str(settings.db_path))
    restored.row_factory = sqlite3.Row
    try:
        kinds = {row["id"]: row["kind"] for row in restored.execute("SELECT id, kind FROM agents")}
        assert kinds["worker-a"] == "service"
        assert restored.execute("SELECT COUNT(*) FROM schema_migrations WHERE revision = 30").fetchone()[0] == 1
    finally:
        restored.close()

