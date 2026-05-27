from pathlib import Path


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
        assert {"transmission", "google_gmail", "github_cli"}.issubset(ids)
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
        assert installed_path.exists()

        library_r2 = test_client.get(
            "/api/connector-types/adapters",
            headers={"Authorization": f"Bearer {admin_token}"},
        )
        transmission2 = next(
            a for a in library_r2.json()["data"]["adapters"] if a["id"] == "transmission"
        )
        assert transmission2["installed"] is False
