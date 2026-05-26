from typing import Any, Optional

from app.connectors.errors import (
    AuthExpiredError,
    ProviderError,
    RateLimitedError,
    SessionExpiredError,
)
from app.connectors.base import Credential

__all__ = [
    "AuthExpiredError",
    "Credential",
    "ProviderError",
    "RateLimitedError",
    "SessionExpiredError",
]


class BaseConnector:
    connector_type_id: str = ""
    needs_session: bool = False

    def test_connection(self, credential: Any, config_json: Optional[str]) -> dict:
        raise NotImplementedError

    def execute(
        self,
        action: str,
        params: dict,
        credential: Any,
        config_json: Optional[str],
        session: Optional[dict] = None,
    ) -> dict:
        raise NotImplementedError

    def refresh_session(
        self,
        credential: Any,
        config_json: Optional[str],
        current_session: Optional[dict],
    ) -> dict:
        raise NotImplementedError(
            "Subclass must implement refresh_session if needs_session is True"
        )


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
