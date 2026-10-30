import time
import struct
from typing import Optional

import httpx

from app.services.vector_settings_service import (
    get_vector_provider,
    get_vector_url,
    get_vector_model,
    get_vector_auth_type,
    get_vector_setting,
    is_vector_search_enabled,
)


_cached_status: Optional[dict] = None
_cache_timestamp: float = 0.0
_CACHE_TTL = 60.0
# One embedding call may take this long before the write that needed it goes on
# without a vector (text search still finds the record).
EMBED_TIMEOUT_SECONDS = 30


def _reset_cache() -> None:
    global _cached_status, _cache_timestamp
    _cached_status = None
    _cache_timestamp = 0.0


def auth_headers_for(auth_type: Optional[str], api_key: Optional[str]) -> dict[str, str]:
    if not api_key or not auth_type or auth_type == "none":
        return {}
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {api_key}"}
    if auth_type == "api_key":
        return {"X-API-Key": api_key}
    return {}


def _auth_headers() -> dict[str, str]:
    return auth_headers_for(get_vector_auth_type(), get_vector_setting("vector_api_key"))


def headers_for_endpoint(
    url: str, auth_type: Optional[str] = None, api_key: Optional[str] = None
) -> dict[str, str]:
    """Auth for a request the settings form makes before anything is saved.

    A key typed into the form goes with that form's URL. The stored key belongs
    to the saved endpoint and is only ever sent there, never to a URL typed into
    the form but not saved.
    """
    if api_key:
        return auth_headers_for(auth_type or "bearer", api_key)
    if url.strip().rstrip("/") == get_vector_url().strip().rstrip("/"):
        return _auth_headers()
    return {}


class EmbeddingRequestError(Exception):
    """The backend answered, but not with an embedding. `code` is a short status."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def request_embedding(
    provider: str,
    url: str,
    model: str,
    text: str,
    headers: Optional[dict] = None,
    timeout: float = EMBED_TIMEOUT_SECONDS,
) -> list[float]:
    """One embedding from either API. Raises EmbeddingRequestError or httpx errors."""
    url = url.rstrip("/")
    if provider == "openai":
        resp = httpx.post(
            f"{url}/embeddings",
            json={"model": model, "input": text},
            headers=headers or {},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise EmbeddingRequestError(f"openai_error:{resp.status_code}")
        items = (resp.json() or {}).get("data") or []
        embedding = items[0].get("embedding") if items and isinstance(items[0], dict) else None
    else:
        resp = httpx.post(
            f"{url}/api/embeddings",
            json={"model": model, "prompt": text},
            headers=headers or {},
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise EmbeddingRequestError(f"ollama_error:{resp.status_code}")
        embedding = (resp.json() or {}).get("embedding")
    if not embedding:
        raise EmbeddingRequestError("no_embedding_in_response")
    return embedding


def list_models(provider: str, url: str, headers: Optional[dict] = None, timeout: float = 5) -> list[str]:
    """Model names the endpoint reports. Raises EmbeddingRequestError or httpx errors.

    Not filtered to embedding models: neither API says reliably which ones are,
    and guessing would hide a usable one.
    """
    url = url.rstrip("/")
    if provider == "openai":
        resp = httpx.get(f"{url}/models", headers=headers or {}, timeout=timeout)
        if resp.status_code != 200:
            raise EmbeddingRequestError(f"openai_error:{resp.status_code}")
        items = (resp.json() or {}).get("data") or []
        names = [m.get("id") for m in items if isinstance(m, dict)]
    else:
        resp = httpx.get(f"{url}/api/tags", headers=headers or {}, timeout=timeout)
        if resp.status_code != 200:
            raise EmbeddingRequestError(f"ollama_error:{resp.status_code}")
        items = (resp.json() or {}).get("models") or []
        names = [m.get("name") for m in items if isinstance(m, dict)]
    return [n for n in names if isinstance(n, str) and n]


def _model_available(provider: str, url: str, model: str, headers: dict) -> bool:
    if provider == "openai":
        # Some OpenAI-compatible services list only part of what they serve,
        # so a model missing from /models gets one small embedding to settle it.
        try:
            if model in list_models(provider, url, headers):
                return True
        except Exception:
            pass
        try:
            request_embedding(provider, url, model, "ping", headers, timeout=10)
            return True
        except Exception:
            return False
    resp = httpx.post(
        f"{url.rstrip('/')}/api/show", json={"name": model}, headers=headers, timeout=5
    )
    return resp.status_code == 200


def get_embedding_backend_status() -> dict:
    global _cached_status, _cache_timestamp
    now = time.monotonic()
    if _cached_status is not None and (now - _cache_timestamp) < _CACHE_TTL:
        return _cached_status

    backend = "unavailable"
    model_configured = False
    vector_enabled = is_vector_search_enabled()
    provider = get_vector_provider()

    if not vector_enabled:
        _cached_status = {
            "backend": "disabled",
            "model_configured": False,
            "model": get_vector_model(),
            "provider": provider,
            "vector_url": get_vector_url(),
            "vector_search_enabled": False,
        }
        _cache_timestamp = now
        return _cached_status

    url = get_vector_url()
    model = get_vector_model()
    headers = _auth_headers()
    try:
        list_models(provider, url, headers)
        backend = "healthy"
    except Exception:
        pass

    if backend == "healthy":
        try:
            model_configured = _model_available(provider, url, model, headers)
        except Exception:
            pass

    _cached_status = {
        "backend": backend,
        "model_configured": model_configured,
        "model": model,
        "provider": provider,
        "vector_url": url,
        "vector_search_enabled": vector_enabled,
    }
    _cache_timestamp = now
    return _cached_status


def generate_embedding(text: str) -> tuple[Optional[bytes], str]:
    status = get_embedding_backend_status()
    if status["backend"] != "healthy" or not status["model_configured"]:
        return None, "unavailable"

    try:
        embedding = request_embedding(
            get_vector_provider(), get_vector_url(), get_vector_model(), text, _auth_headers()
        )
        vector_bytes = struct.pack(f"{len(embedding)}f", *embedding)
        return vector_bytes, "ok"
    except EmbeddingRequestError as e:
        return None, e.code
    except Exception as e:
        return None, f"exception:{type(e).__name__}"


def backend_label(status: dict) -> str:
    return status.get("backend", "unknown")


def retrieval_is_degraded(status: dict) -> bool:
    """True when search fell back to text matching because embeddings are unusable."""
    return status.get("backend") != "healthy" or not status.get("model_configured", False)


def safe_backend_status() -> dict:
    """Backend status that never raises — search must not fail because a probe did."""
    try:
        return get_embedding_backend_status()
    except Exception:
        return {"backend": "error", "model_configured": False}
