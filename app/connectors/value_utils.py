"""Small value transforms shared by declarative connector engines."""

import json
from typing import Any, Optional


def parse_json_object(value: Optional[str]) -> dict:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def extract_jsonpath(path: str, body: Any) -> Any:
    if not path.startswith("$."):
        return body
    current = body
    for part in path[2:].split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def decode_json_body(text: str):
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text
