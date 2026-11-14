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
    def test_connector_catalog_read_does_not_scan_adapter_manifests(
        self, clean_db, monkeypatch
    ):
        def fail_if_scanned():
            raise AssertionError("service catalog reads must not scan adapter files")

        monkeypatch.setattr(adapter_loader, "list_available_adapters", fail_if_scanned)

        types = connector_service.list_connector_types()

        assert any(connector_type["id"] == "generic_http" for connector_type in types)

    def test_adapter_manifest_scan_is_cached_until_a_manifest_changes(
        self, clean_db, monkeypatch, tmp_path
    ):
        from app.config import settings

        system_root = tmp_path / "system-adapters"
        _write_adapter_manifest(system_root, "1.0.0", "Transmission v1")
        monkeypatch.setattr(adapter_loader, "SYSTEM_ADAPTER_DIR", system_root)
        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path))
        adapter_loader.clear_adapter_source_cache()

        original_scan = adapter_loader._scan_root
        scan_calls = 0

        def count_scans(*args, **kwargs):
            nonlocal scan_calls
            scan_calls += 1
            return original_scan(*args, **kwargs)

        monkeypatch.setattr(adapter_loader, "_scan_root", count_scans)

        first = adapter_loader.list_available_adapters()
        second = adapter_loader.list_available_adapters()

        assert scan_calls == 2  # system root and user root, once each
        assert first == second

        _write_adapter_manifest(system_root, "1.1.0", "Transmission v2")
        refreshed = adapter_loader.list_available_adapters()

        assert scan_calls == 4
        assert refreshed[0]["version"] == "1.1.0"

    def test_library_lists_built_in_templates(self, test_client, admin_token):
        r = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]
        adapters = data["adapters"]
        ids = {a["id"] for a in adapters}
        assert {"transmission", "google_workspace", "github_cli", "securo"}.issubset(ids)
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
        securo = next(adapter for adapter in adapters if adapter["id"] == "securo")
        assert securo["requirements_summary"]["config"] == ["base_url"]
        assert securo["requirements_summary"]["credential_fields"] == ["api_key"]

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

    def test_uninstalling_an_adapter_whose_shipped_template_is_gone_keeps_its_only_copy(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        """An adapter installed from a shipped template that a later release dropped has no template to fall back to:
        removing it from the service list must not delete the installed copy, or it could never be added again."""
        from app.config import settings
        from app.services import connector_service

        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)
        headers = {"Authorization": f"Bearer {admin_token}"}
        assert test_client.post("/api/connector-types/adapters/transmission/install", headers=headers, json={}).status_code == 201
        installed = Path(tmp_path) / "adapters" / "transmission" / "adapter.json"
        assert installed.exists()

        real = adapter_loader._source_entries

        def without_the_shipped_copy():
            system, user = real()
            return [e for e in system if e["id"] != "transmission"], user

        monkeypatch.setattr(adapter_loader, "_source_entries", without_the_shipped_copy)
        adapter_loader.clear_adapter_source_cache()
        response = test_client.delete("/api/connector-types/adapters/transmission/install", headers=headers)
        assert response.status_code == 200, response.text
        assert connector_service.get_connector_type("transmission") is None
        assert installed.exists()  # the only copy is kept

        library = test_client.get("/api/connector-types/adapters", headers=headers).json()["data"]["adapters"]
        entry = next(a for a in library if a["id"] == "transmission")
        assert entry["installed"] is False and entry["shipped"] is False  # listed as an added adapter, installable again
        assert test_client.post("/api/connector-types/adapters/transmission/install", headers=headers, json={}).status_code == 201

    def test_old_update_backups_are_pruned_the_newest_kept_for_an_installed_adapter(
        self, test_client, admin_token, monkeypatch, tmp_path
    ):
        import os
        import time

        from app.config import settings

        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)
        headers = {"Authorization": f"Bearer {admin_token}"}
        assert test_client.post("/api/connector-types/adapters/transmission/install", headers=headers, json={}).status_code == 201
        root = Path(tmp_path) / "adapters"

        def make(name, age_hours):
            folder = root / name
            folder.mkdir()
            (folder / "adapter.json").write_text("{}")
            stamp = time.time() - age_hours * 3600
            os.utime(folder, (stamp, stamp))
            return folder

        old, newer = make(".transmission.backup-aaaa", 72), make(".transmission.backup-bbbb", 5)
        gone_a, gone_b = make(".paperclip.backup-cccc", 100), make(".paperclip.backup-dddd", 3)  # not installed
        stale_staging, fresh_staging = make(".transmission.staging-old", 30), make(".transmission.staging-new", 1)

        removed = adapter_loader.prune_adapter_backups()
        assert removed == 4
        assert newer.exists() and not old.exists()                       # newest kept for an installed adapter
        assert not gone_a.exists() and not gone_b.exists()               # an uninstalled adapter keeps none
        assert not stale_staging.exists() and fresh_staging.exists()     # a day-old half-made copy goes, a fresh one stays
        assert (root / "transmission" / "adapter.json").exists()          # the installed tree is never touched

        # uninstalling clears its backups too
        assert test_client.delete("/api/connector-types/adapters/transmission/install", headers=headers).status_code == 200
        assert not newer.exists()

    def test_maintenance_reports_the_adapter_backups_it_pruned(self, test_client, admin_token, monkeypatch, tmp_path):
        from app.config import settings
        from app.services import backup_service

        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path), raising=False)
        leftover = Path(tmp_path) / "adapters" / ".ghost.backup-abcd"
        leftover.mkdir(parents=True)
        result = backup_service.run_scheduled_maintenance()
        assert result["adapter_backups_pruned"] == 1 and not leftover.exists()

    @pytest.mark.parametrize("adapter_id", ["lifequery", "healthquery"])
    def test_lifequery_and_healthquery_are_built_in_and_an_older_install_updates_in_place(
        self, adapter_id, test_client, admin_token, monkeypatch, tmp_path
    ):
        """The two public-product services ship with Agent Core again: they read "Built-in", and an install made from
        the earlier copy (version 1.0.0) is offered Update, which keeps its connections."""
        import shutil

        from app.adapter_paths import SYSTEM_ADAPTER_DIR as shipped_root
        from app.config import settings

        headers = {"Authorization": f"Bearer {admin_token}"}
        assert (shipped_root / adapter_id / "adapter.json").exists()
        shipped = json.loads((shipped_root / adapter_id / "adapter.json").read_text())
        assert shipped["version"] == "1.0.1" and all("side_effect" in a for a in shipped["actions"])
        assert shipped["backend"]["test_action"] in {a["name"] for a in shipped["actions"] if a["side_effect"] == "read"}

        # an install made from the earlier copy
        older_root = tmp_path / "older-templates"
        shutil.copytree(shipped_root / adapter_id, older_root / adapter_id)
        older = json.loads((older_root / adapter_id / "adapter.json").read_text())
        older["version"] = "1.0.0"
        (older_root / adapter_id / "adapter.json").write_text(json.dumps(older))
        monkeypatch.setattr(adapter_loader, "SYSTEM_ADAPTER_DIR", older_root, raising=False)
        monkeypatch.setattr(settings, "DATA_PATH", str(tmp_path / "data"), raising=False)
        adapter_loader.clear_adapter_source_cache()
        assert test_client.post(f"/api/connector-types/adapters/{adapter_id}/install", headers=headers, json={}).status_code == 201
        binding = connector_service.create_binding(
            connector_type_id=adapter_id, name=f"{adapter_id} connection", scope="workspace:test",
            config_json=json.dumps({"base_url": "http://example.test"}),
        )

        # the shipped template is the newer one
        monkeypatch.setattr(adapter_loader, "SYSTEM_ADAPTER_DIR", shipped_root, raising=False)
        adapter_loader.clear_adapter_source_cache()
        entry = next(a for a in test_client.get("/api/connector-types/adapters", headers=headers).json()["data"]["adapters"] if a["id"] == adapter_id)
        assert entry["shipped"] is True  # the card reads "Built-in", not "Added here"
        assert entry["installed_version"] == "1.0.0" and entry["available_version"] == "1.0.1" and entry["update_available"] is True

        updated = test_client.post(f"/api/connector-types/adapters/{adapter_id}/update", headers=headers, json={})
        assert updated.status_code == 200, updated.text
        assert updated.json()["data"]["adapter"]["installed_version"] == "1.0.1"
        kept = connector_service.list_bindings(connector_type_id=adapter_id)
        assert [b["id"] for b in kept] == [binding["id"]]
        assert kept[0]["config_json"] and json.loads(kept[0]["config_json"])["base_url"] == "http://example.test"

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
