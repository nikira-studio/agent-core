import re


ID_NORMALIZATION_REGEX = re.compile(r"^[a-z0-9_-]{1,64}$")


def normalize_id(value: str) -> str:
    normalized = value.lower().strip()
    if not ID_NORMALIZATION_REGEX.match(normalized):
        raise ValueError(
            f"Invalid ID format: '{value}'. Must be 1-64 chars, lowercase a-z, 0-9, hyphen, underscore only."
        )
    return normalized


def validate_id(value: str) -> bool:
    """True when `value` is an acceptable id. The non-raising form of normalize_id."""
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

# Kinds only a signed-in person can write: Core refuses them from an agent, so a record with one of these was
# written by a person. Every other kind is something an agent reported.
HUMAN_SOURCE_KINDS = frozenset({"operator_authored", "human_direct"})

RECORD_STATUSES = ("active", "superseded", "retracted")

ACTIVITY_STATUSES = (
    "active",
    "stale",
    "reassigned",
    "completed",
    "blocked",
    "cancelled",
)

USER_ROLES = ("admin", "user")

# "capability:<connection id>" is the owner of a credential made for one connection: no agent can reach it (it is in
# no agent's scopes), only the hub, an administrator, and a person who may manage that connection.
SCOPE_PREFIXES = ("user", "agent", "workspace", "shared", "system", "capability")
