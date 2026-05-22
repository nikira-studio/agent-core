from typing import Optional


class BaseConnector:
    connector_type_id: str = ""

    def test_connection(self, credential: str, config_json: Optional[str]) -> dict:
        raise NotImplementedError

    def execute(
        self, action: str, params: dict, credential: str, config_json: Optional[str]
    ) -> dict:
        raise NotImplementedError


_CONNECTOR_REGISTRY: dict[str, type[BaseConnector]] = {}


def register_connector(connector_type_id: str, cls: type[BaseConnector]) -> None:
    _CONNECTOR_REGISTRY[connector_type_id] = cls


def get_connector(connector_type_id: str) -> Optional[BaseConnector]:
    cls = _CONNECTOR_REGISTRY.get(connector_type_id)
    if cls is None:
        return None
    return cls()


def list_registered_connectors() -> list[str]:
    return list(_CONNECTOR_REGISTRY.keys())
