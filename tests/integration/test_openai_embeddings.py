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
    assert r.status_code == 502 and "needs an API key" in r.json()["error"]["message"]


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


class _HtmlResp:
    status_code = 200

    def json(self):
        raise ValueError("not json")


def _gateway(monkeypatch):
    """A gateway like New API: a web page at the root, a token-checked API under /v1."""
    def fake_get(url, headers=None, timeout=None):
        if "/v1/" in url:
            if (headers or {}).get("Authorization") == "Bearer good":
                return _Resp(200, {"data": [{"id": "nomic-embed-text"}]})
            return _Resp(401, {"error": {"message": "Invalid token"}})
        return _HtmlResp()

    def fake_post(url, json=None, headers=None, timeout=None):
        if url.endswith("/v1/embeddings") and (headers or {}).get("Authorization") == "Bearer good":
            return _Resp(200, {"data": [{"embedding": [0.1, 0.2]}]})
        return _Resp(401, {"error": {"message": "Invalid token"}})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)


def test_connection_test_says_the_url_needs_v1(clean_db, monkeypatch):
    from app.services.vector_settings_service import test_vector_connection

    _gateway(monkeypatch)
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test:4000", model="nomic-embed-text")

    result = test_vector_connection()

    assert result["success"] is False
    assert "not with the embedding API" in result["error"]
    assert "http://gateway.test:4000/v1" in result["error"]


def test_connection_test_says_a_key_is_needed(clean_db, monkeypatch):
    from app.services.vector_settings_service import test_vector_connection

    _gateway(monkeypatch)
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test:4000/v1",
                         model="nomic-embed-text", auth_type="none")

    result = test_vector_connection()

    assert result["success"] is False and "needs an API key" in result["error"]
    assert "Invalid token" not in result["error"]  # upstream text is not echoed


def test_connection_test_passes_with_v1_and_a_key(clean_db, monkeypatch):
    from app.services.vector_settings_service import test_vector_connection

    _gateway(monkeypatch)
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test:4000/v1",
                         model="nomic-embed-text", auth_type="bearer", api_key="good")
    assert test_vector_connection()["success"] is True


def test_unreachable_backend_says_so(clean_db, monkeypatch):
    from app.services.vector_settings_service import test_vector_connection

    def refuse(*a, **k):
        raise httpx.ConnectError("refused")

    monkeypatch.setattr(httpx, "get", refuse)
    save_vector_settings(enabled=True, provider="ollama", url="http://nowhere.test:11434", model="m")
    assert test_vector_connection()["error"] == (
        "Cannot reach the embedding backend at http://nowhere.test:11434 (ConnectError).")


def test_model_list_route_explains_a_missing_v1(admin_client, monkeypatch):
    _gateway(monkeypatch)
    r = admin_client.post("/api/dashboard/vector-models",
                          json={"provider": "openai", "url": "http://gateway.test:4000"})
    assert r.status_code == 502
    assert "usually end in /v1" in r.json()["error"]["message"]


def test_connection_test_embeds_for_real_and_reports_overload(clean_db, monkeypatch):
    """A gateway can list the model and still refuse to embed."""
    from app.services.vector_settings_service import test_vector_connection

    def fake_get(url, headers=None, timeout=None):
        return _Resp(200, {"data": [{"id": "nomic-embed-text"}]})

    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json)
        return _Resp(503, {"error": {"message": "system cpu overloaded"}})

    monkeypatch.setattr(httpx, "get", fake_get)
    monkeypatch.setattr(httpx, "post", fake_post)
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test/v1", model="nomic-embed-text")

    result = test_vector_connection()

    assert result["success"] is False
    assert "embedding failed" in result["error"] and "overloaded" in result["error"]
    assert sent == [{"model": "nomic-embed-text", "input": "connection test"}]


def _refusing_gateway(monkeypatch, status, payload):
    """Lists the model, then refuses every embedding with `status` and `payload`."""
    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _Resp(
        200, {"data": [{"id": "snowflake-arctic-embed2"}]}))
    monkeypatch.setattr(httpx, "post", lambda url, json=None, headers=None, timeout=None: _Resp(status, payload))
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test/v1",
                         model="snowflake-arctic-embed2")
    embedding_service._reset_cache()


def test_connection_test_shows_the_gateways_reason(clean_db, monkeypatch):
    """A gateway's own rejection, such as a word filter, is not visible in the status alone."""
    from app.services.vector_settings_service import test_vector_connection

    _refusing_gateway(monkeypatch, 500, {"error": {
        "message": "user sensitive words detected: test_sensitive", "code": "sensitive_words_detected"}})

    error = test_vector_connection()["error"]

    assert "answered with an error (500)" in error
    assert error.endswith("The backend said: user sensitive words detected: test_sensitive")


def test_reembed_failure_status_carries_the_reason(clean_db, monkeypatch):
    _refusing_gateway(monkeypatch, 400, {"error": "input length exceeds the context length"})
    assert embedding_service.generate_embedding("x") == (
        None, "openai_error:400 (input length exceeds the context length)")


@pytest.mark.parametrize("payload, detail", [
    ({"detail": "no channel for this model"}, "no channel for this model"),
    ({"message": "  spread \n over\tlines "}, "spread over lines"),
    ({"error": {"message": "x" * 500}}, "x" * 197 + "..."),
    ({"error": {"code": 7}}, ""),
    ({}, ""),
    (["not", "a", "dict"], ""),
])
def test_error_detail_shapes(payload, detail):
    assert embedding_service._error_detail(_Resp(500, payload)) == detail
    assert embedding_service._error_detail(_HtmlResp()) == ""


def test_refused_key_detail_is_never_kept(clean_db, monkeypatch):
    _refusing_gateway(monkeypatch, 401, {"error": {"message": "Incorrect API key provided: sk-ab***yz"}})
    with pytest.raises(embedding_service.EmbeddingRequestError) as e:
        embedding_service.request_embedding("openai", "http://gateway.test/v1", "m", "x")
    assert e.value.code == "unauthorized:401" and e.value.detail == ""


def test_timeout_after_connecting_says_slow_not_unreachable(clean_db, monkeypatch):
    from app.services.vector_settings_service import test_vector_connection

    def slow(*a, **k):
        raise httpx.ReadTimeout("timed out")

    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _Resp(
        200, {"data": [{"id": "m"}]}))
    monkeypatch.setattr(httpx, "post", slow)
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test/v1", model="m")

    error = test_vector_connection()["error"]

    assert "did not answer within 30 seconds (ReadTimeout)" in error
    assert "Cannot reach" not in error
    assert embedding_service.describe_error("exception:ConnectTimeout", "openai", "http://g/v1") == (
        "Cannot reach the embedding backend at http://g/v1 (ConnectTimeout).")


def _capture_inputs(monkeypatch):
    sent = []

    def fake_post(url, json=None, headers=None, timeout=None):
        sent.append(json["input"])
        return _Resp(200, {"data": [{"embedding": [0.1, 0.2]}]})

    monkeypatch.setattr(httpx, "get", lambda url, headers=None, timeout=None: _Resp(
        200, {"data": [{"id": "m"}]}))
    monkeypatch.setattr(httpx, "post", fake_post)
    return sent


def test_query_prefix_goes_on_searches_only(clean_db, monkeypatch):
    from app.services import memory_service

    sent = _capture_inputs(monkeypatch)
    save_vector_settings(enabled=True, provider="openai", url="http://gateway.test/v1", model="m",
                         query_prefix="query: ")
    embedding_service._reset_cache()

    memory_service.write_memory("The build server runs nightly.", "fact", "workspace:prefix-test")
    memory_service.search_memory("build server", ["workspace:prefix-test"])

    assert "The build server runs nightly." in sent
    assert "query: build server" in sent
    assert not any(s.startswith("query: The build") for s in sent)


def test_query_prefix_is_saved_from_the_form_and_shown(admin_client):
    r = admin_client.post("/api/dashboard/vector-settings", json={
        "vector_provider": "openai", "vector_url": "http://gateway.test/v1", "vector_model": "m",
        "vector_auth_type": "none", "vector_query_prefix": "query: "})
    assert r.status_code == 200
    assert r.json()["data"]["settings"]["vector_query_prefix"] == "query: "
    assert 'id="vector-query-prefix" value="query: "' in admin_client.get("/settings").text

    r = admin_client.post("/api/dashboard/vector-settings", json={
        "vector_provider": "openai", "vector_url": "http://gateway.test/v1", "vector_model": "m",
        "vector_auth_type": "none", "vector_query_prefix": "q" * 101})
    assert r.status_code == 400
