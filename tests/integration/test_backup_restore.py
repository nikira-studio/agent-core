import pytest


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
