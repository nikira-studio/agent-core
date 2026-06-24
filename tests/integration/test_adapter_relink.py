"""sync_installed_adapters must self-heal a stale absolute source_path.

Reproduces the production case where an adapter install record stored an
absolute path captured before the data volume was remounted at a different
location (host /srv/docker-data/core vs container /data). The manifest still
exists at the canonical user-adapter location, so restore should relink to it
rather than warn and drop the connector.
"""

from pathlib import Path

from app.database import get_db
from app.services import adapter_loader, connector_service


def _set_source_path(adapter_id: str, source_path: str) -> None:
    with get_db() as conn:
        conn.execute(
            "UPDATE adapter_installations SET source_path = ? WHERE adapter_id = ?",
            (source_path, adapter_id),
        )
        conn.commit()


def _get_source_path(adapter_id: str) -> str:
    with get_db() as conn:
        row = conn.execute(
            "SELECT source_path FROM adapter_installations WHERE adapter_id = ?",
            (adapter_id,),
        ).fetchone()
    return row["source_path"]


def test_sync_relinks_stale_source_path(test_client, admin_token, monkeypatch, tmp_path):
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

    install_r = test_client.post(
        "/api/connector-types/adapters/transmission/install",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert install_r.status_code == 201, install_r.text

    canonical = Path(tmp_path) / "adapters" / "transmission"
    assert (canonical / "adapter.json").exists()

    # Simulate the stale-path state: the record points at an old absolute path
    # that does not exist (as if the data dir moved), but the manifest is still
    # present at the canonical location.
    _set_source_path("transmission", "/old/host/path/adapters/transmission")
    assert connector_service.delete_connector_type("transmission") is True
    assert connector_service.get_connector_type("transmission") is None

    adapter_loader.sync_installed_adapters()

    # Connector restored from the canonical manifest...
    assert connector_service.get_connector_type("transmission") is not None
    # ...and the record was healed to the current canonical path.
    assert _get_source_path("transmission") == str(canonical)


def test_sync_warns_when_manifest_truly_missing(
    test_client, admin_token, monkeypatch, tmp_path, caplog
):
    from app.config import settings

    monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

    install_r = test_client.post(
        "/api/connector-types/adapters/transmission/install",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={},
    )
    assert install_r.status_code == 201, install_r.text

    # Remove the manifest entirely and point the record at a dead path: nothing
    # to relink to, so restore should skip it (not crash).
    import shutil

    shutil.rmtree(Path(tmp_path) / "adapters" / "transmission")
    _set_source_path("transmission", "/old/host/path/adapters/transmission")
    connector_service.delete_connector_type("transmission")

    adapter_loader.sync_installed_adapters()
    assert connector_service.get_connector_type("transmission") is None
