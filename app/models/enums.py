import re
from typing import Any


ID_NORMALIZATION_REGEX = re.compile(r"^[a-z0-9_-]{1,64}$")


def normalize_id(value: str) -> str:
    normalized = value.lower().strip()
    if not ID_NORMALIZATION_REGEX.match(normalized):
        raise ValueError(
            f"Invalid ID format: '{value}'. Must be 1-64 chars, lowercase a-z, 0-9, hyphen, underscore only."
        )
    return normalized


def validate_id(value: str) -> bool:
    try:
        normalize_id(value)
        return True
    except ValueError:
        return False


MEMORY_CLASSES = ("fact", "preference", "decision", "scratchpad")

SOURCE_KINDS = (
    "operator_authored",
    "human_direct",
    "tool_output",
    "agent_inference",
    "episodic_inference",
    "semantic_inference",
    "external_import",
)

RECORD_STATUSES = ("active", "superseded", "retracted", "held")

VALUE_TYPES = ("api", "password", "url", "config", "other")

ACTIVITY_STATUSES = ("active", "stale", "reassigned", "completed", "blocked", "cancelled")

USER_ROLES = ("admin", "user")

SCOPE_PREFIXES = ("user", "agent", "workspace", "shared", "system")

BOOLEAN_TRUE = ("true", "1", "yes")
BOOLEAN_FALSE = ("false", "0", "no")


def is_boolean_true(value: str) -> bool:
    return value.lower() in BOOLEAN_TRUE


def is_boolean_false(value: str) -> bool:
    return value.lower() in BOOLEAN_FALSE
