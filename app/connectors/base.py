import json
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Credential:
    """Resolved credential. `.raw` = decrypted secret string (single-secret connectors).
    `.fields` = parsed object when the stored secret is a JSON blob (oauth/basic/cookie)."""

    raw: Optional[str]
    fields: dict = field(default_factory=dict)
    reference_name: Optional[str] = None
    entry_id: Optional[str] = None

    @classmethod
    def from_resolved(cls, raw: Optional[str], entry: Optional[dict]) -> "Credential":
        parsed: dict = {}
        if raw:
            try:
                obj = json.loads(raw)
                if isinstance(obj, dict):
                    parsed = obj
            except (json.JSONDecodeError, TypeError):
                parsed = {}
        return cls(
            raw=raw,
            fields=parsed,
            reference_name=(entry or {}).get("reference_name"),
            entry_id=(entry or {}).get("id"),
        )

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def __bool__(self) -> bool:
        return bool(self.raw)
