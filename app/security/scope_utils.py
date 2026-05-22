from app.models.enums import normalize_id, SCOPE_PREFIXES


def validate_scope_string(scope: str) -> bool:
    if not scope:
        return False
    if scope in ("shared", "system"):
        return True
    parts = scope.split(":", 1)
    if len(parts) != 2:
        return False
    prefix, value = parts
    if prefix not in SCOPE_PREFIXES:
        return False
    if not value:
        return False
    if prefix in ("user", "agent", "workspace"):
        if any(c.isupper() for c in value):
            return False
        try:
            normalize_id(value)
        except ValueError:
            return False
    return True


def normalize_scope_string(scope: str) -> str:
    parts = scope.split(":", 1)
    if len(parts) == 2:
        prefix, value = parts
        if prefix in ("user", "agent", "workspace"):
            try:
                value = normalize_id(value)
            except ValueError:
                pass
        return f"{prefix}:{value}"
    return scope.lower()
