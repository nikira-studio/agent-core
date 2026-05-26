"""Integration tests for adapter discovery from data/adapters/ manifests."""

import json
import tempfile
from pathlib import Path


from app.connectors.manifest import load_and_validate


class TestAdapterDiscovery:
    def test_good_manifest_seeds_connector_type(self, clean_db):
        from app.services import adapter_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter_dir = Path(tmpdir) / "my_test_adapter"
            adapter_dir.mkdir()
            manifest = {
                "spec_version": "1.0",
                "id": "my_test_adapter",
                "display_name": "My Test Adapter",
                "version": "1.0.0",
                "description": "A test adapter",
                "actions": [
                    {
                        "name": "do_thing",
                        "description": "Does the thing",
                        "side_effect": "read",
                    },
                    {
                        "name": "undo_thing",
                        "description": "Undoes the thing",
                        "side_effect": "destructive",
                    },
                ],
                "backend": {
                    "type": "http",
                    "requests": {
                        "do_thing": {
                            "method": "GET",
                            "path": "/things",
                            "response": {"success_when": "$.ok == true"},
                        },
                    },
                },
            }
            (adapter_dir / "adapter.json").write_text(json.dumps(manifest))

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            from app.services import connector_service

            ct = connector_service.get_connector_type("my_test_adapter")
            assert ct is not None
            assert ct["display_name"] == "My Test Adapter"
            assert ct["backend_type"] == "http"

            actions = ct.get("supported_actions", [])
            names = [a["name"] if isinstance(a, dict) else a for a in actions]
            assert "do_thing" in names
            assert "undo_thing" in names

    def test_bad_manifest_is_skipped(self, clean_db):
        from app.services import adapter_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter_dir = Path(tmpdir) / "bad_adapter"
            adapter_dir.mkdir()
            (adapter_dir / "adapter.json").write_text(
                json.dumps({"spec_version": "1.0", "id": "bad", "version": "1.0.0"})
            )

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            from app.services import connector_service

            ct = connector_service.get_connector_type("bad")
            assert ct is None

    def test_unknown_backend_type_skipped(self, clean_db):
        from app.services import adapter_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter_dir = Path(tmpdir) / "ftp_adapter"
            adapter_dir.mkdir()
            manifest = {
                "spec_version": "1.0",
                "id": "ftp_adapter",
                "version": "1.0.0",
                "backend": {"type": "ftp", "requests": {}},
            }
            (adapter_dir / "adapter.json").write_text(json.dumps(manifest))

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            from app.services import connector_service

            ct = connector_service.get_connector_type("ftp_adapter")
            assert ct is None

    def test_adapter_with_unmet_requirement_seeded_as_unavailable(
        self, clean_db
    ):
        """An adapter that declares a hard operator-level requirement (a binary
        that must be installed) and the requirement is not met is seeded as
        unavailable. Note: `requires.config` is NOT a hard gate — config fields
        are per-binding, validated at binding create/execute time, so they don't
        prevent adapter discovery. `requires.bins`/`requires.env` are the hard
        gates."""
        from app.services import adapter_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter_dir = Path(tmpdir) / "needs_binary"
            adapter_dir.mkdir()
            manifest = {
                "spec_version": "1.0",
                "id": "needs_binary",
                "display_name": "Needs Binary Adapter",
                "version": "1.0.0",
                "description": "Needs a binary the operator must install",
                "requires": {"bins": ["definitely_not_a_real_binary_xyz_abc_123"]},
                "actions": [{"name": "ping", "side_effect": "read"}],
                "backend": {"type": "http", "requests": {}},
            }
            (adapter_dir / "adapter.json").write_text(json.dumps(manifest))

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            from app.services import connector_service

            ct = connector_service.get_connector_type("needs_binary")
            assert ct is not None
            assert ct["display_name"] == "Needs Binary Adapter"
            assert "[unavailable]" in ct["description"]

    def test_multiple_adapters_discovered(self, clean_db):
        from app.services import adapter_loader

        with tempfile.TemporaryDirectory() as tmpdir:
            for adapter_id in ["adapter_a", "adapter_b"]:
                adapter_dir = Path(tmpdir) / adapter_id
                adapter_dir.mkdir()
                manifest = {
                    "spec_version": "1.0",
                    "id": adapter_id,
                    "display_name": f"Adapter {adapter_id}",
                    "version": "1.0.0",
                    "actions": [{"name": "act1", "side_effect": "read"}],
                    "backend": {"type": "http", "requests": {}},
                }
                (adapter_dir / "adapter.json").write_text(json.dumps(manifest))

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            from app.services import connector_service

            for adapter_id in ["adapter_a", "adapter_b"]:
                ct = connector_service.get_connector_type(adapter_id)
                assert ct is not None

    def test_adapter_update_overwrites_existing(self, clean_db):
        from app.services import adapter_loader
        from app.services import connector_service

        with tempfile.TemporaryDirectory() as tmpdir:
            adapter_dir = Path(tmpdir) / "updatable"
            adapter_dir.mkdir()
            manifest_v1 = {
                "spec_version": "1.0",
                "id": "updatable",
                "display_name": "Updatable Adapter",
                "version": "1.0.0",
                "description": "Version 1",
                "actions": [{"name": "act1", "side_effect": "read"}],
                "backend": {"type": "http", "requests": {}},
            }
            (adapter_dir / "adapter.json").write_text(json.dumps(manifest_v1))

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            ct1 = connector_service.get_connector_type("updatable")
            assert ct1["display_name"] == "Updatable Adapter"
            assert ct1["description"] == "Version 1"

            manifest_v2 = {
                **manifest_v1,
                "version": "2.0.0",
                "description": "Version 2",
            }
            (adapter_dir / "adapter.json").write_text(json.dumps(manifest_v2))

            adapter_loader.discover_and_seed_adapters(adapters_dir=tmpdir)

            ct2 = connector_service.get_connector_type("updatable")
            assert ct2["description"] == "Version 2"


class TestManifestLoaderIntegration:
    def test_load_and_validate_transmission_manifest(self):
        path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/transmission/adapter.json"
        )
        m, err = load_and_validate(path)
        assert err is None, f"Expected no error, got: {err}"
        assert m is not None
        assert m.id == "transmission"
        assert m.spec_version == "1.0"
        assert len(m.actions) == 6
        action_names = [a["name"] for a in m.actions]
        assert "list_torrents" in action_names
        assert "remove_torrent" in action_names

    def test_all_transmission_actions_have_side_effect(self):
        path = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters/transmission/adapter.json"
        )
        m, err = load_and_validate(path)
        assert err is None
        for action in m.actions:
            assert "side_effect" in action, (
                f"Action {action['name']} missing side_effect"
            )
        side_effects = {a["name"]: a["side_effect"] for a in m.actions}
        assert side_effects["list_torrents"] == "read"
        assert side_effects["remove_torrent"] == "destructive"
        assert side_effects["add_torrent"] == "write"
        assert side_effects["start_torrent"] == "write"
        assert side_effects["stop_torrent"] == "write"
