"""The OpenAI-compatible embedding provider, and what replaced "generic"."""

import httpx
import pytest

from app.services import embedding_service, reembed_service
from app.services.vector_settings_service import save_vector_settings


class _Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload


@pytest.fixture
def openai_backend(clean_db, monkeypatch):
    """A fake OpenAI-compatible service at https://embed.example/v1."""
    calls = []
    state = {"listed": ["text-embedding-3-small", "gpt-4o"], "status": 200}

    def fake_get(url, headers=None, timeout=None):
        calls.append(("GET", url, dict(headers or {}), None))
        if url.endswith("/models"):
            return _Resp(state["status"], {"data": [{"id": m} for m in state["listed"]]})
        return _Resp(404, {})

    def fake_post(url, json=None, headers=None, timeout=None):
        calls.append(("POST", url, dict(headers or {}), json))
        if url.endswith("/embeddings") and "input" in (json or {}):
            return _Resp(200, {"data": [{"embedding": [0.25, 0.5, 0.75]}]})
        return _Resp(404, {})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    save_vector_settings(enabled=True, provider="openai", url="https://embed.example/v1",
                         model="text-embedding-3-small", auth_type="bearer", api_key="sk-saved")
    embedding_service._reset_cache()
    return calls, state


def test_openai_provider_embeds_through_the_embeddings_api(openai_backend):
    calls, _ = openai_backend

    vector, status = embedding_service.generate_embedding("hello")

    assert status == "ok" and len(vector) == 12  # three float32 values
    posts = [c for c in calls if c[0] == "POST"]
    assert posts[-1][1] == "https://embed.example/v1/embeddings"
    assert posts[-1][3] == {"model": "text-embedding-3-small", "input": "hello"}
    assert posts[-1][2] == {"Authorization": "Bearer sk-saved"}
    assert not any("/api/" in c[1] for c in calls)  # nothing Ollama-specific


def test_openai_status_lists_models_and_finds_the_configured_one(openai_backend):
    calls, _ = openai_backend
    status = embedding_service.get_embedding_backend_status()
    assert status["backend"] == "healthy" and status["model_configured"] is True
    assert status["provider"] == "openai"
    assert not [c for c in calls if c[0] == "POST"]  # listed, so no probe embedding


def test_openai_model_missing_from_the_list_is_settled_by_one_embedding(openai_backend):
    calls, state = openai_backend
    state["listed"] = ["gpt-4o"]  # a service that lists only part of what it serves

    status = embedding_service.get_embedding_backend_status()

    assert status["model_configured"] is True
    assert [c[1] for c in calls if c[0] == "POST"] == ["https://embed.example/v1/embeddings"]


def test_openai_backend_that_refuses_the_key_is_unavailable(openai_backend):
    _, state = openai_backend
    state["status"] = 401
    assert embedding_service.get_embedding_backend_status()["backend"] == "unavailable"
    assert embedding_service.generate_embedding("hello") == (None, "unavailable")


def test_saved_key_only_goes_to_the_saved_url(openai_backend):
    assert embedding_service.headers_for_endpoint("https://embed.example/v1/") == {
        "Authorization": "Bearer sk-saved"}
    assert embedding_service.headers_for_endpoint("https://other.example/v1") == {}
    assert embedding_service.headers_for_endpoint("https://other.example/v1", "api_key", "typed") == {
        "X-API-Key": "typed"}


def test_estimate_measures_an_openai_model(openai_backend):
    calls, _ = openai_backend
    est = reembed_service.estimate(provider="openai")
    assert est["ok"] and est["dimension"] == 3
    assert [c[1] for c in calls if c[0] == "POST"] == ["https://embed.example/v1/embeddings"] * 2


@pytest.fixture
def admin_client(test_client, clean_db):
    from app.services.auth_service import create_session, create_user

    create_user("admin", "admin@test.local", "testpassword123", "Admin", "admin")
    test_client.cookies.set("session_token", create_session("admin", channel="dashboard")["session_id"])
    return test_client


def test_model_list_route_for_openai(admin_client, openai_backend):
    calls, _ = openai_backend
    r = admin_client.post("/api/dashboard/vector-models",
                          json={"provider": "openai", "url": "https://embed.example/v1"})
    assert r.status_code == 200
    assert r.json()["data"] == {"models": ["text-embedding-3-small", "gpt-4o"], "provider": "openai"}
    assert calls[-1][2] == {"Authorization": "Bearer sk-saved"}

    # A URL that is not saved gets the key typed into the form, never the saved one.
    admin_client.post("/api/dashboard/vector-models", json={"provider": "openai", "url": "https://new.example/v1"})
    assert calls[-1][1] == "https://new.example/v1/models" and calls[-1][2] == {}
    admin_client.post("/api/dashboard/vector-models", json={
        "provider": "openai", "url": "https://new.example/v1", "auth_type": "bearer", "api_key": "sk-typed"})
    assert calls[-1][2] == {"Authorization": "Bearer sk-typed"}


def test_model_list_route_reports_failures(admin_client, openai_backend):
    _, state = openai_backend
    assert admin_client.post("/api/dashboard/vector-models", json={"url": "ftp://x"}).status_code == 400
    assert admin_client.post("/api/dashboard/vector-models",
                             json={"url": "https://embed.example/v1", "provider": "cohere"}).status_code == 400
    state["status"] = 401
    r = admin_client.post("/api/dashboard/vector-models", json={"url": "https://embed.example/v1"})
    assert r.status_code == 502 and "openai_error:401" in r.json()["error"]["message"]


def test_model_list_route_for_ollama(admin_client, monkeypatch):
    seen = []

    def fake_get(url, headers=None, timeout=None):
        seen.append(url)
        return _Resp(200, {"models": [{"name": "nomic-embed-text:latest"}]})

    monkeypatch.setattr(httpx, "get", fake_get)
    r = admin_client.post("/api/dashboard/vector-models", json={"provider": "ollama", "url": "http://ollama.test:11434"})
    assert r.json()["data"]["models"] == ["nomic-embed-text:latest"]
    assert seen == ["http://ollama.test:11434/api/tags"]


def test_settings_page_offers_ollama_and_openai_only(admin_client):
    html = admin_client.get("/settings").text
    select = html.split('id="vector-provider"')[1].split("</select>")[0]
    assert ">OpenAI-compatible<" in select and ">Ollama<" in select and "generic" not in select.lower()
    assert "window.loadVectorModels = loadVectorModels" in html
