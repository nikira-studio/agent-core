"""Shared parameter validation for connector actions and connection probes."""

import logging
from typing import Optional

logger = logging.getLogger(__name__)


def validate_action_params(
    connector_type: dict, action: str, params: Optional[dict]
) -> Optional[dict]:
    """Validate caller params against the action's declared input_schema before
    executing. Returns an error dict to abort, or None to proceed.

    This is a safety gate: it stops malformed, missing, or empty required params
    from ever reaching the connector. It matters most for destructive actions
    whose backend treats an omitted/empty selector as "apply to ALL" (e.g.
    Transmission torrent-remove with no ids removes the entire queue). Declaring
    the selector required with minItems>=1 turns "remove all by accident" into a
    rejected call.
    """
    meta = None
    for a in connector_type.get("supported_actions") or []:
        if isinstance(a, dict) and a.get("name") == action:
            meta = a
            break
    schema = (meta or {}).get("input_schema")
    if not isinstance(schema, dict):
        return None  # nothing declared to validate against
    try:
        import jsonschema

        jsonschema.validate(instance=params or {}, schema=schema)
    except jsonschema.ValidationError as e:
        return {
            "success": False,
            "error": f"Invalid parameters for '{action}': {e.message}",
            "error_code": "INVALID_PARAMS",
        }
    except Exception:
        logger.exception("param schema validation failed to run for action %s", action)
        return {
            "success": False,
            "error": f"Invalid input schema for '{action}'",
            "error_code": "INVALID_SCHEMA",
        }
    return None
