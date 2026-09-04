from app.connectors import get_connector, register_connector, resolve_connector


class LegacyConnector:
    def __init__(self):
        self.created = True


class MetadataConnector:
    def __init__(self, metadata):
        self.metadata = metadata


def test_legacy_zero_argument_connector_registration(monkeypatch):
    from app import connectors

    monkeypatch.setattr(connectors, "_CONNECTOR_REGISTRY", {})
    register_connector("legacy", LegacyConnector)

    assert get_connector("legacy").created is True
    assert isinstance(resolve_connector({"id": "legacy"}), LegacyConnector)


def test_metadata_connector_registration(monkeypatch):
    from app import connectors

    monkeypatch.setattr(connectors, "_CONNECTOR_REGISTRY", {})
    register_connector("metadata", MetadataConnector)
    metadata = {"id": "metadata", "backend_type": None}

    assert resolve_connector(metadata).metadata is metadata
