from collections.abc import Callable
import inspect
from typing import Any, Optional, Protocol

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


class BaseConnector(Protocol):
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


ConnectorFactory = Callable[[dict], BaseConnector]
ConnectorRegistration = Callable[[], BaseConnector] | ConnectorFactory
_CONNECTOR_REGISTRY: dict[str, ConnectorFactory] = {}


def register_connector(
    connector_type_id: str, factory: ConnectorRegistration
) -> None:
    """Register either a metadata factory or a legacy zero-argument class."""
    try:
        inspect.signature(factory).bind({})
    except (TypeError, ValueError):
        try:
            inspect.signature(factory).bind()
        except (TypeError, ValueError) as exc:
            raise TypeError(
                "connector factory must accept connector metadata or no arguments"
            ) from exc
        _CONNECTOR_REGISTRY[connector_type_id] = lambda _metadata: factory()
    else:
        _CONNECTOR_REGISTRY[connector_type_id] = factory


def get_connector(
    connector_type_id: str, connector_type: Optional[dict] = None
) -> Optional[BaseConnector]:
    factory = _CONNECTOR_REGISTRY.get(connector_type_id)
    if factory is None:
        return None
    return factory(connector_type or {"id": connector_type_id})


def _generic_http_factory(connector_type: dict) -> BaseConnector:
    from app.connectors.generic_http import GenericHttpConnector

    return GenericHttpConnector()


def _http_factory(connector_type: dict) -> BaseConnector:
    from app.connectors.http_engine import HttpEngine

    return HttpEngine(connector_type)


def _openapi_factory(connector_type: dict) -> BaseConnector:
    from app.connectors.openapi_executor import OpenApiExecutor

    return OpenApiExecutor()


def _cli_factory(connector_type: dict) -> BaseConnector:
    from app.connectors.cli_engine import CliEngine

    return CliEngine(connector_type)


_BACKEND_FACTORIES: dict[str, ConnectorFactory] = {
    "generic_http": _generic_http_factory,
    "http": _http_factory,
    "openapi": _openapi_factory,
    "cli": _cli_factory,
}


def resolve_connector(connector_type: dict) -> Optional[BaseConnector]:
    """Resolve a connector-specific factory, then its declared backend."""
    factory = _CONNECTOR_REGISTRY.get(connector_type["id"])
    if factory:
        return factory(connector_type)
    backend = connector_type.get("backend_type")
    if not backend:
        # Rows from before backend_type are normalized by schema migration, but
        # imports and hand-built test databases may still reach this boundary.
        if connector_type.get("provider_type") == "mcp":
            backend = "mcp"
        elif connector_type.get("operations_json"):
            backend = "openapi"
        elif connector_type.get("id") == "generic_http":
            backend = "generic_http"
    factory = _BACKEND_FACTORIES.get(backend)
    return factory(connector_type) if factory else None
