"""Wire-level tests for the unified Google Workspace adapter.

Covers the things that make a multi-service connector work: per-request host
routing (one adapter spanning gmail/www/sheets/docs/people hosts), optional
query-param omission, and the default-fallback render fix.
"""

import json
from pathlib import Path

from app.connectors.base import Credential
from app.connectors.http_engine import HttpEngine

ADAPTER = Path(__file__).parent.parent.parent / "app" / "adapter_templates" / "google_workspace" / "adapter.json"


def _engine() -> HttpEngine:
    m = json.load(open(ADAPTER))
    return HttpEngine({"id": "google_workspace", "backend_json": json.dumps(m["backend"])})


def _cred() -> Credential:
    return Credential(raw=None, fields={"access_token": "T", "client_id": "c", "client_secret": "s"})


def _req(action, params):
    eng = _engine()
    return eng._build_request(eng.spec["requests"][action], params, {}, _cred())


class TestGoogleWorkspaceManifest:
    def test_manifest_validates(self):
        from app.connectors import manifest as mf

        man, err = mf.load_and_validate(ADAPTER)
        assert err is None, err
        assert man is not None and man.id == "google_workspace"

    def test_scopes_cover_all_six_services(self):
        m = json.load(open(ADAPTER))
        scopes = m["backend"]["auth"]["authorization"]["scopes"]
        for needle in ("gmail", "calendar", "drive", "contacts", "spreadsheets", "documents"):
            assert any(needle in s for s in scopes), f"missing scope for {needle}"


class TestMultiHostRouting:
    def test_each_service_targets_its_own_host(self):
        cases = {
            "send_email": ("gmail.googleapis.com", {"to": ["a@b.c"], "subject": "s", "body": "b"}),
            "list_events": ("www.googleapis.com/calendar/v3", {}),
            "list_files": ("www.googleapis.com/drive/v3", {}),
            "list_contacts": ("people.googleapis.com/v1", {}),
            "get_values": ("sheets.googleapis.com/v4", {"spreadsheet_id": "S", "range": "A1"}),
            "get_document": ("docs.googleapis.com/v1", {"document_id": "D"}),
        }
        for action, (host_fragment, params) in cases.items():
            url = _req(action, params)["url"]
            assert host_fragment in url, f"{action} -> {url} (expected {host_fragment})"
            # base_url must not be prepended to an absolute path
            assert url.count("https://") == 1, f"{action} double-base: {url}"


class TestOptionalQueryParams:
    def test_omitted_optionals_are_dropped(self):
        url = _req("list_events", {})["url"]
        assert "__AGENT_CORE_OMIT__" not in url
        assert "timeMin" not in url and "q=" not in url
        assert "maxResults=25" in url  # required default still present

    def test_provided_optional_appears(self):
        url = _req("list_events", {"time_min": "2026-06-07T00:00:00Z"})["url"]
        assert "timeMin=2026-06-07" in url


class TestRenderFixes:
    def test_insert_text_default_index_is_one(self):
        # default(1, as=int) must yield 1, not the int zero-value (Docs needs >=1)
        body = _req("insert_text", {"document_id": "D", "text": "Hi"})["body"]
        assert body["requests"][0]["insertText"]["location"]["index"] == 1

    def test_create_event_omits_unset_optionals(self):
        body = _req("create_event", {"summary": "S", "start": "2026-06-08T15:00:00Z", "end": "2026-06-08T15:30:00Z"})["body"]
        assert "description" not in body and "location" not in body
        assert body["start"] == {"dateTime": "2026-06-08T15:00:00Z"}

    def test_sheets_append_targets_append_endpoint_with_values(self):
        req = _req("append_values", {"spreadsheet_id": "S", "range": "Sheet1!A1", "values": [["a", "b"]]})
        assert req["url"].startswith("https://sheets.googleapis.com/v4/spreadsheets/S/values/Sheet1!A1:append")
        assert req["body"]["values"] == [["a", "b"]]


def _decode_raw(raw_b64: str) -> str:
    import base64
    return base64.urlsafe_b64decode(raw_b64 + "=" * (-len(raw_b64) % 4)).decode("utf-8")


class TestRfc822Builder:
    def test_cc_bcc_render_as_headers_before_body(self):
        # regression: cc/bcc used to be appended *after* the body (invalid)
        body = _req("send_email", {"to": ["a@b.c"], "cc": ["c@d.e"], "bcc": ["f@g.h"],
                                    "subject": "Hi", "body": "the body"})["body"]
        raw = _decode_raw(body["raw"])
        head, _, msg = raw.partition("\r\n\r\n")
        assert "Cc: c@d.e" in head and "Bcc: f@g.h" in head
        assert msg == "the body" and "Cc:" not in msg

    def test_reply_sets_thread_and_in_reply_to(self):
        req = _req("reply", {"to": ["a@b.c"], "subject": "Re: x", "body": "hi",
                             "thread_id": "T123", "in_reply_to": "<msg@id>"})
        assert req["body"]["threadId"] == "T123"
        head = _decode_raw(req["body"]["raw"]).partition("\r\n\r\n")[0]
        assert "In-Reply-To: <msg@id>" in head and "References: <msg@id>" in head
