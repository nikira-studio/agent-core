"""Artifact export for connector-generated payloads.

Connectors that produce large base64 image responses (notably OpenRouter
image generation, which returns a 1-2 MB `data:image/png;base64,...` body)
cannot safely hand the raw bytes back to the agent: the result would either
balloon the context window or get spilled to Agent Core's encrypted
`tool_result_spill` table, where `result_fetch`'s 50,000-character cap makes
it unrecoverable.

This service:

- Detects `data:image/<subtype>;base64,<...>` strings anywhere inside a
  connector response body (recursive walk over dicts, lists, and bare
  strings).
- Decodes the base64 payload, writes the raw bytes to
  `data/artifacts/connector/<handle>.<ext>`, and returns a small reference
  object the caller can use to find the file.
- Replaces each data URL with that reference object so the response that
  reaches the agent only carries a path, mime type, and byte count.

Non-image data URLs and oversized non-data-URL payloads are not handled
here; they continue to use the existing `tool_result_spill` path. No
decoded bytes are ever written to logs.
"""

import base64
import binascii
import json
import logging
import re
import secrets
from typing import Any, Optional

from app.config import settings

logger = logging.getLogger(__name__)


_DATA_URL_RE = re.compile(
    r"^data:image/(?P<subtype>[A-Za-z0-9.+-]+);base64,(?P<b64>[A-Za-z0-9+/=]+)$",
    re.IGNORECASE,
)

_ALLOWED_SUBTYPES = {
    "png": "png",
    "jpeg": "jpg",
    "jpg": "jpg",
    "webp": "webp",
    "gif": "gif",
    "svg+xml": "svg",
    "bmp": "bmp",
    "tiff": "tiff",
}


def is_image_data_url(value: Any) -> bool:
    """True when `value` is a string that looks like a data URL image."""
    if not isinstance(value, str):
        return False
    return _DATA_URL_RE.match(value) is not None


def _parse_data_url(value: str) -> Optional[tuple[bytes, str, str]]:
    """Return (bytes, mime_type, extension) for a valid image data URL.

    The match is strict: only `data:image/<sub>;base64,...` where the
    subtype is in the allow-list and the base64 payload decodes without
    error. Whitespace inside the payload is tolerated so a copy-pasted URL
    with stray newlines still parses. Returns None on any failure so the
    caller can leave the original string in place.
    """
    match = _DATA_URL_RE.match(value)
    if not match:
        return None
    subtype = match.group("subtype").lower()
    if subtype not in _ALLOWED_SUBTYPES:
        return None
    ext = _ALLOWED_SUBTYPES[subtype]
    mime = f"image/{subtype}"
    b64 = "".join(match.group("b64").split())
    try:
        raw = base64.b64decode(b64, validate=True)
    except (binascii.Error, ValueError):
        return None
    if not raw:
        return None
    return raw, mime, ext


def _artifact_dir() -> "Any":
    # Imported lazily so the test that monkeypatches DATA_PATH after import
    # still sees the right location.
    path = settings.data_dir / "artifacts" / "connector"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _export_one(value: str) -> dict:
    """Decode a single image data URL and write the bytes to disk.

    On any decode or write failure, returns a small object with
    `exported: False` so the caller can tell the value was inspected but
    not persisted. The original value is never re-emitted in the result
    because the caller is the one that already has it.
    """
    parsed = _parse_data_url(value)
    if parsed is None:
        return {
            "exported": False,
            "reason": "data_url_invalid_or_unsupported",
        }
    raw, mime, ext = parsed
    handle = secrets.token_urlsafe(16)
    target = _artifact_dir() / f"{handle}.{ext}"
    try:
        target.write_bytes(raw)
    except OSError as exc:
        logger.warning(
            "artifact_export_service: failed to write %s: %s", target, exc
        )
        return {
            "exported": False,
            "reason": "disk_write_failed",
            "error": str(exc),
        }
    return {
        "exported": True,
        "artifact": {
            "path": str(target),
            "mime_type": mime,
            "size_bytes": len(raw),
            "handle": handle,
            "format": "data_url",
        },
    }


def export_image_data_urls(value: Any) -> tuple[Any, int]:
    """Recursively walk a decoded payload and export any data URL images.

    `value` is whatever the connector returned in its `body` field: a dict,
    list, or scalar. Each `data:image/...;base64,...` string encountered is
    replaced with the reference object produced by `_export_one`. Returns
    the (possibly modified) value and a count of successful exports so the
    caller can log/audit.
    """
    count = 0

    def _walk(node: Any) -> Any:
        nonlocal count
        if isinstance(node, dict):
            return {k: _walk(v) for k, v in node.items()}
        if isinstance(node, list):
            return [_walk(v) for v in node]
        if is_image_data_url(node):
            ref = _export_one(node)
            if ref.get("exported"):
                count += 1
                logger.info(
                    "artifact_export_service: exported data URL image to %s (%d bytes)",
                    ref["artifact"]["path"],
                    ref["artifact"]["size_bytes"],
                )
            else:
                # Inspect happened but export failed: keep the original
                # value so the caller still gets the bytes through the
                # existing spill path rather than dropping the payload.
                return node
            return ref
        return node

    return _walk(value), count


def export_data_url_string(value: str) -> tuple[Any, int]:
    """Export image data URLs from a string body.

    Two shapes are common:
    1. The body IS the data URL (some APIs return the image as the
       response body). In that case the whole string is replaced with a
       single reference.
    2. The body is JSON whose nested string fields contain data URLs
       (e.g. OpenRouter's `{"choices": [{"message": {"content":
       "data:image/png;base64,..."}}]}`). The JSON is parsed, walked, and
       re-serialized.
    """
    if is_image_data_url(value):
        ref = _export_one(value)
        if ref.get("exported"):
            return ref, 1
        return value, 0
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        return value, 0
    if not isinstance(parsed, (dict, list)):
        return value, 0
    new_value, count = export_image_data_urls(parsed)
    if count == 0:
        return value, 0
    return json.dumps(new_value), count


def export_connector_body(body: Any) -> tuple[Any, int]:
    """Top-level entry point: export data URLs in any connector body shape.

    The HTTP engine and MCP provider both pass through bodies that may be
    a parsed JSON object, a JSON string, a plain text response, or — for
    image generation endpoints — a data URL string. This dispatches to
    the right walker and returns the transformed body and a count.
    """
    if isinstance(body, (dict, list)):
        return export_image_data_urls(body)
    if isinstance(body, str):
        return export_data_url_string(body)
    return body, 0
