"""A model is a capability, never a dependency.

Every test here is really the same assertion from a different angle: the system
works without one, and configuring one only adds. Nothing in memory, search,
credentials or connectors may require it.
"""

import json

from app.database import get_db
from app.services import model_service


def _configure(**rows):
    with get_db() as conn:
        for key, value in rows.items():
            conn.execute(
                "INSERT OR REPLACE INTO system_settings (key, value) VALUES (?, ?)",
                (key, str(value)),
            )
        conn.commit()


# --- unconfigured is a supported state -------------------------------------


def test_a_fresh_install_has_no_model(clean_db):
    assert model_service.is_available() is False
    assert model_service.get_config()["provider"] == ""


def test_asking_without_a_model_returns_nothing_rather_than_raising(clean_db):
    assert model_service.complete("anything") is None


def test_the_core_system_works_without_one(clean_db):
    """Memory, search and retrieval must not depend on a model being present."""
    from app.services import memory_service

    assert model_service.is_available() is False
    record, _ = memory_service.write_memory(
        content="The ingest route is POST /api/webhook/health.",
        memory_class="fact",
        scope="workspace:proj",
    )
    found, _ = memory_service.search_memory("ingest route", ["workspace:proj"])
    assert [r["id"] for r in found] == [record["id"]]


def test_an_incomplete_configuration_is_not_available(clean_db):
    _configure(review_model_provider="ollama", review_model_url="http://localhost:11434")
    assert model_service.is_available() is False, "a URL with no model name is not usable"
    assert "URL or model name is missing" in model_service.describe_unavailable()


def test_an_unknown_provider_is_ignored(clean_db):
    _configure(review_model_provider="something-else")
    assert model_service.get_config()["provider"] == ""
    assert model_service.is_available() is False


# --- reaching a model ------------------------------------------------------


def test_a_local_endpoint_is_called_directly(clean_db, monkeypatch):
    """The local path talks to the endpoint, so nothing leaves the machine."""
    seen = {}

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": '{"ok": true}'}}

    def fake_post(url, json=None, timeout=None):
        seen["url"] = url
        seen["payload"] = json
        return _Response()

    monkeypatch.setattr(model_service.httpx, "post", fake_post)
    _configure(
        review_model_provider="ollama",
        review_model_url="http://localhost:11434",
        review_model_name="qwen3.5:4b",
    )

    assert model_service.complete("hello") == '{"ok": true}'
    assert seen["url"] == "http://localhost:11434/api/chat"
    assert seen["payload"]["model"] == "qwen3.5:4b"
    assert seen["payload"]["stream"] is False


def test_a_binding_is_used_when_configured(clean_db, monkeypatch):
    from app.services import connector_service

    seen = {}

    def fake(binding_id, action, params=None):
        seen.update({"binding_id": binding_id, "action": action})
        return {
            "success": True,
            "data": {"choices": [{"message": {"content": '{"ok": true}'}}]},
        }

    monkeypatch.setattr(connector_service, "execute_binding_action_with_logging", fake)
    _configure(review_model_provider="binding", review_model_binding_id="b-1")

    assert model_service.complete("hello") == '{"ok": true}'
    assert seen["binding_id"] == "b-1"


def test_a_failing_model_degrades_quietly(clean_db, monkeypatch):
    """A bad day for the model must not become an error for the caller."""

    def boom(*args, **kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(model_service.httpx, "post", boom)
    _configure(
        review_model_provider="ollama",
        review_model_url="http://localhost:11434",
        review_model_name="qwen3.5:4b",
    )
    assert model_service.complete("hello") is None


# --- reading answers -------------------------------------------------------


def test_json_is_extracted_from_surrounding_prose(clean_db):
    assert model_service.extract_json('Sure!\n{"verdict": "keep"}\nHope that helps.') == {
        "verdict": "keep"
    }


def test_unreadable_answers_are_not_guessed_at(clean_db):
    for reply in (None, "", "no json here", "{not json}"):
        assert model_service.extract_json(reply) is None


# --- validation is explicit ------------------------------------------------


def test_validation_reports_an_unconfigured_model(clean_db):
    result = model_service.validate()
    assert result["ok"] is False
    assert "No review model configured" in result["error"]


def test_validation_reports_an_unreachable_model(clean_db, monkeypatch):
    monkeypatch.setattr(
        model_service.httpx, "post", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("nope"))
    )
    _configure(
        review_model_provider="ollama",
        review_model_url="http://localhost:11434",
        review_model_name="m",
    )
    result = model_service.validate()
    assert result["ok"] is False
    assert "could not be reached" in result["error"]


def test_validation_rejects_a_model_that_cannot_follow_a_format(clean_db, monkeypatch):
    """These features need structured answers; better to find out at setup."""

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": "Sure, I am working fine!"}}

    monkeypatch.setattr(model_service.httpx, "post", lambda *a, **k: _Response())
    _configure(
        review_model_provider="ollama",
        review_model_url="http://localhost:11434",
        review_model_name="m",
    )
    result = model_service.validate()
    assert result["ok"] is False
    assert "usable JSON" in result["error"]


def test_validation_passes_for_a_working_model(clean_db, monkeypatch):
    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"message": {"content": json.dumps({"ok": True})}}

    monkeypatch.setattr(model_service.httpx, "post", lambda *a, **k: _Response())
    _configure(
        review_model_provider="ollama",
        review_model_url="http://localhost:11434",
        review_model_name="qwen3.5:4b",
    )
    result = model_service.validate()
    assert result["ok"] is True
    assert result["model"] == "qwen3.5:4b"


def test_the_validation_endpoint_is_admin_only(test_client, agent_token):
    r = test_client.post(
        "/api/dashboard/review-model/test",
        headers={"Authorization": f"Bearer {agent_token}"},
    )
    assert r.status_code in (401, 403)


def test_the_validation_endpoint_reports_status(test_client, admin_token):
    r = test_client.post(
        "/api/dashboard/review-model/test",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["ok"] is False, "nothing configured on a fresh install"


# --- the settings surface --------------------------------------------------


def test_the_settings_page_offers_a_review_model(test_client, admin_token):
    """It was configurable only by writing to the database by hand."""
    r = test_client.get("/settings", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "Review Model" in r.text
    assert "review-model-provider" in r.text
    assert "testReviewModel()" in r.text
    # The page must say plainly that the rest of the system does not need it.
    assert "Everything else works without one" in r.text


def test_the_review_model_can_be_picked_from_the_endpoint(test_client, admin_token):
    """Typing a model name from memory is how you get a silent typo."""
    r = test_client.get("/settings", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    assert "loadReviewModels()" in r.text
    assert 'list="review-model-options"' in r.text
    assert '<datalist id="review-model-options">' in r.text
    assert "/api/dashboard/ollama-models" in r.text
    # It must stay a free-text field: a model the endpoint does not report, or a
    # failed listing, cannot be allowed to block configuration.
    assert 'type="text" id="review-model-name"' in r.text


def test_both_settings_cards_list_models_from_one_endpoint(test_client, admin_token):
    """The embedding and review cards ask the same endpoint the same question."""
    r = test_client.get("/settings", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.text.count("/api/dashboard/ollama-models") == 2
    assert "/api/dashboard/vector-settings/models" not in r.text


def test_saving_a_local_model_through_the_api(test_client, admin_token):
    r = test_client.post(
        "/api/dashboard/review-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "review_model_provider": "ollama",
            "review_model_url": "http://localhost:11434",
            "review_model_name": "qwen3:8b",
            "usefulness_review_enabled": "true",
        },
    )
    assert r.status_code == 200, r.json()
    assert r.json()["data"]["available"] is True
    assert model_service.get_config()["model"] == "qwen3:8b"


def test_a_half_configured_model_is_refused(test_client, admin_token):
    """Storing something that can only fail later helps nobody."""
    r = test_client.post(
        "/api/dashboard/review-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"review_model_provider": "ollama", "review_model_url": "http://localhost:11434"},
    )
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "INCOMPLETE_CONFIG"


def test_clearing_the_provider_turns_the_capability_off(test_client, admin_token):
    test_client.post(
        "/api/dashboard/review-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "review_model_provider": "ollama",
            "review_model_url": "http://localhost:11434",
            "review_model_name": "m",
        },
    )
    assert model_service.is_available() is True

    r = test_client.post(
        "/api/dashboard/review-model",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"review_model_provider": ""},
    )
    assert r.status_code == 200, r.json()
    assert model_service.is_available() is False


def test_saving_the_review_model_is_admin_only(test_client, agent_token):
    r = test_client.post(
        "/api/dashboard/review-model",
        headers={"Authorization": f"Bearer {agent_token}"},
        json={"review_model_provider": ""},
    )
    assert r.status_code in (401, 403)
