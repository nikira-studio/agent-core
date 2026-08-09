"""Unit tests for the connector artifact export service.

The service turns `data:image/<sub>;base64,...` payloads (the kind OpenRouter
image generation returns) into on-disk files plus a small reference object.
These tests cover the three shapes the connector body can take: a dict with
nested data URL strings, a JSON string with the same, and a top-level data
URL string. They also pin the behaviour for non-image data URLs, invalid
base64, and oversized non-data-URL text (which must keep using the existing
spill path and not be touched here).
"""

import base64
import json
from pathlib import Path


def _png_bytes() -> bytes:
    # Real 1x1 PNG, kept tiny so the test stays fast.
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII="
    )


def _data_url(subtype: str = "png", raw: bytes | None = None) -> str:
    raw = raw if raw is not None else _png_bytes()
    return f"data:image/{subtype};base64,{base64.b64encode(raw).decode()}"


def test_is_image_data_url_recognises_supported_subtypes():
    from app.services import artifact_export_service

    assert artifact_export_service.is_image_data_url(_data_url("png"))
    assert artifact_export_service.is_image_data_url(_data_url("jpeg"))
    assert artifact_export_service.is_image_data_url(_data_url("webp"))
    assert artifact_export_service.is_image_data_url(_data_url("svg+xml"))


def test_is_image_data_url_rejects_non_image_and_non_strings():
    from app.services import artifact_export_service

    assert not artifact_export_service.is_image_data_url(
        "data:text/html;base64,PHN0Pjwvc3Q+"
    )
    assert not artifact_export_service.is_image_data_url("https://example.com/img.png")
    assert not artifact_export_service.is_image_data_url(b"data:image/png;base64,xxx")
    assert not artifact_export_service.is_image_data_url(None)
    assert not artifact_export_service.is_image_data_url(42)


def test_export_image_data_urls_walks_nested_dict(clean_db):
    from app.services import artifact_export_service

    payload = {
        "choices": [
            {
                "message": {
                    "content": _data_url("png"),
                    "name": "image-1",
                }
            }
        ],
        "meta": {"trace_id": "abc"},
    }
    new_value, count = artifact_export_service.export_image_data_urls(payload)
    assert count == 1
    exported = new_value["choices"][0]["message"]["content"]
    assert exported["exported"] is True
    assert exported["artifact"]["mime_type"] == "image/png"
    assert exported["artifact"]["size_bytes"] == len(_png_bytes())
    assert Path(exported["artifact"]["path"]).exists()
    assert new_value["meta"]["trace_id"] == "abc"
    # No raw base64 should leak into the returned structure.
    assert "base64," not in json.dumps(new_value)


def test_export_image_data_urls_walks_lists(clean_db):
    from app.services import artifact_export_service

    payload = {"images": [_data_url("png"), _data_url("jpeg")]}
    new_value, count = artifact_export_service.export_image_data_urls(payload)
    assert count == 2
    assert new_value["images"][0]["exported"] is True
    assert new_value["images"][1]["artifact"]["mime_type"] == "image/jpeg"


def test_export_image_data_urls_keeps_invalid_data_url_intact(clean_db):
    from app.services import artifact_export_service

    bad = "data:image/png;base64,not-really-base64@@@"
    payload = {"img": bad, "ok": "data:text/html;base64,abc"}
    new_value, count = artifact_export_service.export_image_data_urls(payload)
    # Bad base64 must NOT be replaced — the caller still gets the bytes via
    # the existing spill path, and the agent can decide what to do.
    assert count == 0
    assert new_value["img"] == bad
    assert new_value["ok"] == "data:text/html;base64,abc"


def test_export_data_url_string_top_level_data_url(clean_db):
    from app.services import artifact_export_service

    url = _data_url("webp")
    new_value, count = artifact_export_service.export_data_url_string(url)
    assert count == 1
    assert new_value["exported"] is True
    assert new_value["artifact"]["mime_type"] == "image/webp"


def test_export_data_url_string_parses_json_body(clean_db):
    from app.services import artifact_export_service

    body = json.dumps(
        {"data": [{"b64_json": _data_url("png"), "index": 0}]}
    )
    new_value, count = artifact_export_service.export_data_url_string(body)
    assert count == 1
    parsed = json.loads(new_value)
    assert parsed["data"][0]["b64_json"]["exported"] is True


def test_export_data_url_string_leaves_plain_json_alone(clean_db):
    from app.services import artifact_export_service

    body = json.dumps({"records": [{"id": 1}, {"id": 2}]})
    new_value, count = artifact_export_service.export_data_url_string(body)
    assert count == 0
    assert new_value == body


def test_export_connector_body_dict_and_string_dispatch(clean_db):
    from app.services import artifact_export_service

    # Dict path
    new_value, count = artifact_export_service.export_connector_body(
        {"img": _data_url("png")}
    )
    assert count == 1
    assert new_value["img"]["exported"] is True

    # String data URL path
    new_value, count = artifact_export_service.export_connector_body(
        _data_url("gif")
    )
    assert count == 1
    assert new_value["exported"] is True

    # Plain text body — not touched, count is 0 so the caller leaves it
    # alone for the spill path.
    new_value, count = artifact_export_service.export_connector_body(
        "this is just a long plain text response with no data urls"
    )
    assert count == 0
    assert new_value == "this is just a long plain text response with no data urls"


def test_large_non_image_string_is_not_handled_by_export(clean_db):
    """Non-image oversized payloads must keep using the spill path.

    This is the regression case from STA-798: a 1-2 MB body without any
    data URL should fall through to the existing tool_result_spill flow
    rather than being silently re-encoded.
    """
    from app.services import artifact_export_service

    big = "x" * (50_000 * 4)  # 200 KB of plain text
    new_value, count = artifact_export_service.export_connector_body(big)
    assert count == 0
    assert new_value == big
    assert len(new_value) == 200_000


def test_exported_artifact_path_is_under_data_dir(clean_db):
    from app.services import artifact_export_service
    from app.config import settings

    payload = {"img": _data_url("png")}
    new_value, _ = artifact_export_service.export_image_data_urls(payload)
    artifact_path = Path(new_value["img"]["artifact"]["path"])
    assert artifact_path.is_file()
    assert artifact_path.parent == settings.data_dir / "artifacts" / "connector"
    assert artifact_path.read_bytes() == _png_bytes()
