import time
import struct
from typing import Optional

import httpx

from app.config import settings


_cached_status: Optional[dict] = None
_cache_timestamp: float = 0.0
_CACHE_TTL = 60.0


def _reset_cache() -> None:
    global _cached_status, _cache_timestamp
    _cached_status = None
    _cache_timestamp = 0.0


def get_embedding_backend_status() -> dict:
    global _cached_status, _cache_timestamp
    now = time.monotonic()
    if _cached_status is not None and (now - _cache_timestamp) < _CACHE_TTL:
        return _cached_status

    backend = "unavailable"
    model_configured = False

    try:
        health_resp = httpx.get(
            f"{settings.OLLAMA_URL}/api/tags",
            timeout=5,
        )
        if health_resp.status_code == 200:
            backend = "healthy"
    except Exception:
        pass

    try:
        model_resp = httpx.post(
            f"{settings.OLLAMA_URL}/api/show",
            json={"name": settings.EMBEDDING_MODEL},
            timeout=5,
        )
        if model_resp.status_code == 200:
            model_configured = True
    except Exception:
        pass

    _cached_status = {
        "backend": backend,
        "model_configured": model_configured,
        "model": settings.EMBEDDING_MODEL,
        "ollama_url": settings.OLLAMA_URL,
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
        resp = httpx.post(
            f"{settings.OLLAMA_URL}/api/embeddings",
            json={"model": settings.EMBEDDING_MODEL, "prompt": text},
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
