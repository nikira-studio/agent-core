import time
import struct
from typing import Optional

import httpx

from app.services.vector_settings_service import (
    get_vector_url,
    get_vector_model,
    get_vector_auth_type,
    get_vector_setting,
    is_vector_search_enabled,
)


_cached_status: Optional[dict] = None
_cache_timestamp: float = 0.0
_CACHE_TTL = 60.0


def _reset_cache() -> None:
    global _cached_status, _cache_timestamp
    _cached_status = None
    _cache_timestamp = 0.0


def _auth_headers() -> dict[str, str]:
    auth_type = get_vector_auth_type()
    api_key = get_vector_setting("vector_api_key")
    if not api_key or auth_type == "none":
        return {}
    if auth_type == "bearer":
        return {"Authorization": f"Bearer {api_key}"}
    if auth_type == "api_key":
        return {"X-API-Key": api_key}
    return {}


def get_embedding_backend_status() -> dict:
    global _cached_status, _cache_timestamp
    now = time.monotonic()
    if _cached_status is not None and (now - _cache_timestamp) < _CACHE_TTL:
        return _cached_status

    backend = "unavailable"
    model_configured = False
    vector_enabled = is_vector_search_enabled()

    if not vector_enabled:
        _cached_status = {
            "backend": "disabled",
            "model_configured": False,
            "model": get_vector_model(),
            "vector_url": get_vector_url(),
            "vector_search_enabled": False,
        }
        _cache_timestamp = now
        return _cached_status

    try:
        ollama_url = get_vector_url()
        health_resp = httpx.get(
            f"{ollama_url}/api/tags",
            headers=_auth_headers(),
            timeout=5,
        )
        if health_resp.status_code == 200:
            backend = "healthy"
    except Exception:
        pass

    try:
        ollama_url = get_vector_url()
        model = get_vector_model()
        model_resp = httpx.post(
            f"{ollama_url}/api/show",
            json={"name": model},
            headers=_auth_headers(),
            timeout=5,
        )
        if model_resp.status_code == 200:
            model_configured = True
    except Exception:
        pass

    _cached_status = {
        "backend": backend,
        "model_configured": model_configured,
        "model": get_vector_model(),
        "vector_url": get_vector_url(),
        "vector_search_enabled": vector_enabled,
    }
    _cache_timestamp = now
    return _cached_status


def is_embedding_backend_healthy() -> bool:
    return get_embedding_backend_status()["backend"] == "healthy"


def generate_embedding(text: str) -> tuple[Optional[bytes], str]:
    status = get_embedding_backend_status()
    if status["backend"] != "healthy" or not status["model_configured"]:
        return None, "unavailable"

    try:
        ollama_url = get_vector_url()
        model = get_vector_model()
        resp = httpx.post(
            f"{ollama_url}/api/embeddings",
            json={"model": model, "prompt": text},
            headers=_auth_headers(),
            timeout=30,
        )
        if resp.status_code != 200:
            return None, f"ollama_error:{resp.status_code}"
        data = resp.json()
        embedding = data.get("embedding")
        if not embedding:
            return None, "no_embedding_in_response"
        vector_bytes = struct.pack(f"{len(embedding)}f", *embedding)
        return vector_bytes, "ok"
    except Exception as e:
        return None, f"exception:{type(e).__name__}"
