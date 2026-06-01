"""Integration tests for connector action schemas from manifests."""

import json



class TestConnectorActionsSchema:
    def test_generates_input_schema_for_http_adapter_actions(self, clean_db):
        from app.database import get_db

        ct_id = "schema_test_http"
        supported_actions = [
            {
                "name": "list_items",
                "description": "List items",
                "side_effect": "read",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "ids": {"type": "array", "items": {"type": "string"}},
                        "limit": {"type": "integer", "default": 10},
                    },
                },
            },
            {
                "name": "delete_item",
                "description": "Delete an item",
                "side_effect": "destructive",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                    },
                    "required": ["id"],
                },
            },
        ]
        backend_json = json.dumps(
            {
                "requests": {
                    "list_items": {"method": "GET", "path": "/items"},
                    "delete_item": {"method": "POST", "path": "/items"},
                },
            }
        )
        with get_db() as conn:
            conn.execute(
                """INSERT INTO connector_types
                   (id, display_name, description, provider_type, auth_type,
                    supported_actions_json, required_credential_fields_json,
                    disabled_actions_json, backend_type, backend_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    ct_id,
                    "Schema Test HTTP",
                    None,
                    "openapi",
                    "bearer",
                    json.dumps(supported_actions),
                    "[]",
                    "[]",
                    "http",
                    backend_json,
                ),
            )
            conn.commit()

        from app.services import connector_service

        result = connector_service.generate_connector_type_tools(
            connector_service.get_connector_type(ct_id),
        )

        tools = {t["name"]: t for t in result["tools"]}
        assert "list_items" in tools
        assert "delete_item" in tools

        list_tool = tools["list_items"]
        assert list_tool["input_schema"]["type"] == "object"
        assert "ids" in list_tool["input_schema"]["properties"]
        assert list_tool["side_effect"] == "read"

        delete_tool = tools["delete_item"]
        assert delete_tool["input_schema"]["type"] == "object"
        assert "id" in delete_tool["input_schema"]["properties"]
        assert delete_tool["side_effect"] == "destructive"

    def test_generate_connector_type_tools_stateless_connector(self, clean_db):
        from app.services import connector_service

        connector_service.create_connector_type(
            connector_type_id="stateless_tool_test",
            display_name="Stateless Tool Test",
            supported_actions=["call_endpoint"],
            provider_type="openapi",
        )

        ct = connector_service.get_connector_type("stateless_tool_test")
        result = connector_service.generate_connector_type_tools(ct)

        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "call_endpoint"

    def test_connector_types_have_backend_type_column(self, clean_db):
        from app.database import get_db

        with get_db() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
            }
        assert "backend_type" in columns

    def test_connector_types_have_backend_json_column(self, clean_db):
        from app.database import get_db

        with get_db() as conn:
            columns = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(connector_types)").fetchall()
            }
        assert "backend_json" in columns

    def test_update_connector_type_actions_accepts_structured_actions(
        self, clean_db, test_client, admin_token
    ):
        from app.services import connector_service

        connector_service.create_connector_type(
            connector_type_id="structured_actions_test",
            display_name="Structured Actions Test",
            supported_actions=[
                {
                    "name": "list_items",
                    "description": "List items",
                    "input_schema": {"type": "object", "properties": {}},
                },
                {
                    "name": "delete_item",
                    "description": "Delete item",
                    "input_schema": {"type": "object", "properties": {}},
                },
            ],
            provider_type="openapi",
        )

        r = test_client.put(
            "/api/connector-types/structured_actions_test/actions",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"disabled_actions": ["delete_item"]},
        )
        assert r.status_code == 200, r.text
        data = r.json()["data"]["connector_type"]
        assert data["disabled_actions"] == ["delete_item"]


class TestTransmissionActionsSchema:
    def test_transmission_adapter_actions_have_schemas(self, clean_db):
        from pathlib import Path
        from app.services import adapter_loader
        from app.services import connector_service

        real_adapters_dir = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters"
        )
        adapter_loader.discover_and_seed_adapters(adapters_dir=real_adapters_dir)

        ct = connector_service.get_connector_type("transmission")
        assert ct is not None

        result = connector_service.generate_connector_type_tools(ct)
        tools = {t["name"]: t for t in result["tools"]}

        assert "list_torrents" in tools
        assert "remove_torrent" in tools
        assert "add_torrent" in tools
        assert "start_torrent" in tools
        assert "stop_torrent" in tools
        assert "get_session_stats" in tools

        assert (
            tools["list_torrents"]["input_schema"]["properties"]["ids"]["type"]
            == "array"
        )
        assert tools["list_torrents"]["side_effect"] == "read"

        assert (
            tools["remove_torrent"]["input_schema"]["properties"]["delete_data"]["type"]
            == "boolean"
        )
        assert tools["remove_torrent"]["side_effect"] == "destructive"

        assert tools["add_torrent"]["side_effect"] == "write"
        assert (
            tools["add_torrent"]["input_schema"]["properties"]["filename"]["type"]
            == "string"
        )

        assert tools["start_torrent"]["side_effect"] == "write"
        assert tools["stop_torrent"]["side_effect"] == "write"

    def test_transmission_adapter_actions_validate_from_structured_manifest(
        self, clean_db
    ):
        from pathlib import Path
        from app.services import adapter_loader
        from app.services import connector_service

        real_adapters_dir = Path(
            "/srv/docker-data/projects/Apps/agent-core/data/adapters"
        )
        adapter_loader.discover_and_seed_adapters(adapters_dir=real_adapters_dir)

        ct = connector_service.get_connector_type("transmission")
        assert ct is not None

        assert (
            connector_service._validate_action_for_connector(ct, "list_torrents")
            is None
        )
        assert (
            connector_service._validate_action_for_connector(ct, "get_session_stats")
            is None
        )
