import json
from pathlib import Path

import pytest

from app.services import adapter_loader, connector_service


def _write_adapter_manifest(root: Path, version: str, description: str) -> Path:
    adapter_dir = root / "transmission"
    adapter_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "spec_version": "1.0",
        "id": "transmission",
        "display_name": "Transmission",
        "version": version,
        "description": description,
        "credential_schema": {
            "fields": [
                {"name": "username", "type": "string", "secret": False, "required": True},
                {"name": "password", "type": "string", "secret": True, "required": True},
            ]
        },
        "requires": {"config": ["base_url"]},
        "actions": [
            {
                "name": "list_torrents",
                "description": "List torrents",
                "side_effect": "read",
                "input_schema": {"type": "object", "properties": {}},
            }
        ],
        "backend": {
            "type": "http",
            "base_url": {"from": "config", "field": "base_url"},
            "requests": {
                "list_torrents": {
                    "method": "POST",
                    "path": "/rpc",
                    "body": {
                        "template": {
                            "method": "torrent-get",
                            "arguments": {
                                "fields": ["id"],
                            },
                        }
                    },
                    "response": {"success_when": "$.result == 'success'"},
                }
            },
        },
    }
    (adapter_dir / "adapter.json").write_text(json.dumps(manifest, indent=2))
    return adapter_dir


class TestAdapterLibrary:
    def test_library_lists_built_in_templates(self, test_client, admin_token):
        r = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        adapters = data["adapters"]
        ids = {a["id"] for a in adapters}
        assert {"transmission", "google_workspace", "github_cli"}.issubset(ids)
        transmission = next(a for a in adapters if a["id"] == "transmission")
        assert transmission["source_kind"] == "system"
        assert transmission["installed"] is False
        assert transmission["requirements_summary"]["config"] == ["base_url"]
        assert transmission["requirements_summary"]["credential_fields"] == [
            "username",
            "password",
        ]
        github_cli = next(a for a in adapters if a["id"] == "github_cli")
        assert github_cli["requirements_summary"]["bins"] == ["gh"]
        workspace = next(a for a in adapters if a["id"] == "google_workspace")
        assert workspace["requirements_summary"]["credential_fields"] == [
            "client_id",
            "client_secret",
        ]
        assert "Authorize OAuth" in workspace["setup"]["instructions"]

    def test_install_and_uninstall_builtin_adapter(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        from app.config import settings
        from app.services import connector_service

        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

        install_r = test_client.post(
            "/api/connector-types/adapters/transmission/install",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        assert install_r.status_code == 201, install_r.text
        install_data = install_r.json()["data"]["adapter"]
        assert install_data["connector_type"]["id"] == "transmission"

        installed_path = Path(tmp_path) / "adapters" / "transmission" / "adapter.json"
        assert installed_path.exists()

        ct = connector_service.get_connector_type("transmission")
        assert ct is not None
        assert ct["provider_type"] == "builtin"
        assert ct["backend_type"] == "http"

        library_r = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert library_r.status_code == 200, library_r.text
        adapters = library_r.json()["data"]["adapters"]
        transmission = next(a for a in adapters if a["id"] == "transmission")
        assert transmission["installed"] is True
        assert transmission["installed_source_kind"] == "system"

        uninstall_r = test_client.delete(
            "/api/connector-types/adapters/transmission/install",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert uninstall_r.status_code == 200, uninstall_r.text
        assert connector_service.get_connector_type("transmission") is None
        assert not installed_path.exists()

        library_r2 = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        transmission2 = next(
            a for a in library_r2.json()["data"]["adapters"] if a["id"] == "transmission"
        )
        assert transmission2["installed"] is False

        reinstall_r = test_client.post(
            "/api/connector-types/adapters/transmission/install",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        assert reinstall_r.status_code == 201, reinstall_r.text
        assert installed_path.exists()

    def test_update_builtin_adapter_refreshes_install_without_losing_bindings(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        from app.config import settings

        system_root = tmp_path / "system-adapters"
        _write_adapter_manifest(system_root, "1.0.0", "Transmission v1")
        monkeypatch.setattr(adapter_loader, "SYSTEM_ADAPTER_DIR", system_root, raising=False)
        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

        install_r = test_client.post(
            "/api/connector-types/adapters/transmission/install",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        assert install_r.status_code == 201, install_r.text

        binding = connector_service.create_binding(
            connector_type_id="transmission",
            name="transmission-binding",
            scope="workspace:test",
        )

        _write_adapter_manifest(system_root, "1.1.0", "Transmission v2")

        library_r = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert library_r.status_code == 200, library_r.text
        transmission = next(
            a for a in library_r.json()["data"]["adapters"] if a["id"] == "transmission"
        )
        assert transmission["installed"] is True
        assert transmission["installed_version"] == "1.0.0"
        assert transmission["available_version"] == "1.1.0"
        assert transmission["update_available"] is True

        update_r = test_client.post(
            "/api/connector-types/adapters/transmission/update",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        assert update_r.status_code == 200, update_r.text
        update_data = update_r.json()["data"]["adapter"]
        assert update_data["previous_version"] == "1.0.0"
        assert update_data["installed_version"] == "1.1.0"
        assert update_data["connector_type"]["id"] == "transmission"

        ct = connector_service.get_connector_type("transmission")
        assert ct is not None

        bindings = connector_service.list_bindings(connector_type_id="transmission")
        assert len(bindings) == 1
        assert bindings[0]["id"] == binding["id"]

        library_r2 = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        transmission2 = next(
            a for a in library_r2.json()["data"]["adapters"] if a["id"] == "transmission"
        )
        assert transmission2["installed_version"] == "1.1.0"
        assert transmission2["available_version"] == "1.1.0"
        assert transmission2["update_available"] is False

    def test_install_failure_rolls_back_system_adapter_tree(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        from app.config import settings

        system_root = tmp_path / "system-adapters"
        _write_adapter_manifest(system_root, "1.0.0", "Transmission v1")
        monkeypatch.setattr(adapter_loader, "SYSTEM_ADAPTER_DIR", system_root, raising=False)
        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

        def boom(*args, **kwargs):
            raise RuntimeError("install failed")

        monkeypatch.setattr(adapter_loader, "_upsert_install_record", boom, raising=False)

        with pytest.raises(RuntimeError, match="install failed"):
            adapter_loader.install_adapter("transmission")

        installed_path = Path(tmp_path) / "adapters" / "transmission"
        assert not installed_path.exists()
        assert connector_service.get_connector_type("transmission") is None

    def test_update_failure_restores_previous_system_adapter_tree(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        from app.config import settings

        system_root = tmp_path / "system-adapters"
        _write_adapter_manifest(system_root, "1.0.0", "Transmission v1")
        monkeypatch.setattr(adapter_loader, "SYSTEM_ADAPTER_DIR", system_root, raising=False)
        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

        install_r = test_client.post(
            "/api/connector-types/adapters/transmission/install",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        assert install_r.status_code == 201, install_r.text

        installed_path = Path(tmp_path) / "adapters" / "transmission" / "adapter.json"
        before = installed_path.read_text()

        _write_adapter_manifest(system_root, "1.1.0", "Transmission v2")

        def boom(*args, **kwargs):
            raise RuntimeError("update failed")

        monkeypatch.setattr(adapter_loader, "_upsert_install_record", boom, raising=False)

        with pytest.raises(RuntimeError, match="update failed"):
            adapter_loader.update_adapter("transmission")

        after = installed_path.read_text()
        assert after == before

        ct = connector_service.get_connector_type("transmission")
        assert ct is not None

        library_entry = adapter_loader.get_adapter_library_entry("transmission")
        assert library_entry is not None
        assert library_entry["installed_version"] == "1.0.0"
        assert library_entry["available_version"] == "1.1.0"
        assert library_entry["update_available"] is True

    def test_delete_service_catalog_adapter_clears_install_state(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        from app.config import settings

        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)

        install_r = test_client.post(
            "/api/connector-types/adapters/transmission/install",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={},
        )
        assert install_r.status_code == 201, install_r.text

        delete_r = test_client.delete(
            "/api/connector-types/transmission",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert delete_r.status_code == 200, delete_r.text

        library_r = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert library_r.status_code == 200, library_r.text
        adapters = library_r.json()["data"]["adapters"]
        transmission = next(a for a in adapters if a["id"] == "transmission")
        assert transmission["installed"] is False
        assert not (Path(tmp_path) / "adapters" / "transmission").exists()
