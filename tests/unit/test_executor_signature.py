"""connector_service calls every executor's execute() with the same keywords, session included."""

import inspect

from app.connectors.cli_engine import CliEngine
from app.connectors.generic_http import GenericHttpConnector
from app.connectors.http_engine import HttpEngine
from app.connectors.openapi_executor import OpenApiExecutor

CALL = {"action", "params", "credential", "config_json", "session"}


def test_every_executor_accepts_the_keywords_the_service_passes():
    for cls in (OpenApiExecutor, HttpEngine, CliEngine, GenericHttpConnector):
        params = set(inspect.signature(cls.execute).parameters) - {"self"}
        assert CALL <= params, f"{cls.__name__}.execute is missing {sorted(CALL - params)}"


def test_an_openapi_action_without_a_spec_fails_cleanly_not_with_a_type_error():
    result = OpenApiExecutor().execute(action="x", params={}, credential=None, config_json=None, session=None)
    assert result == {"success": False, "error": "No spec available"}
