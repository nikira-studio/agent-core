import logging
from typing import Optional
from app.security.encryption import encrypt_value, decrypt_value
from app.services import system_settings_service

logger = logging.getLogger(__name__)

VECTOR_KEYS = (
    "vector_search_enabled",
    "vector_provider",
    "vector_url",
    "vector_model",
    "vector_dimension",
    "vector_auth_type",
)

# "ollama": Ollama's own API (/api/tags, /api/show, /api/embeddings).
# "openai": the OpenAI embeddings API (/models, /embeddings under a base URL
# that usually ends in /v1), which hosted providers and most gateways serve.
VECTOR_PROVIDERS = ("ollama", "openai")
# "generic" was offered before "openai" existed. It only changed the form; every
# call still went to Ollama's endpoints, so an install that saved it is an
# Ollama install, and is read as one.
_PROVIDER_ALIASES = {"generic": "ollama"}


def normalize_provider(value: Optional[str]) -> str:
    value = (value or "ollama").strip().lower()
    return _PROVIDER_ALIASES.get(value, value)


VECTOR_DEFAULTS = {
    "vector_search_enabled": "false",
    "vector_provider": "ollama",
    "vector_url": "http://localhost:11434",
    "vector_model": "nomic-embed-text",
    "vector_dimension": "768",
    "vector_auth_type": "none",
}


def get_vector_settings() -> dict:
    """Settings for display and API responses. Never includes the API key itself,
    only whether one is stored; code that calls the backend reads the key with
    get_vector_setting("vector_api_key")."""
    result = system_settings_service.read_raw(VECTOR_DEFAULTS)
    result["vector_provider"] = normalize_provider(result.get("vector_provider"))
    result["vector_has_api_key"] = bool(_get_stored_api_key_plaintext())
    return result


def get_vector_setting(key: str) -> str:
    if key not in VECTOR_KEYS and key != "vector_api_key":
        raise ValueError(f"Unknown vector setting: {key}")
    if key == "vector_api_key":
        return _get_stored_api_key_plaintext() or ""
    return system_settings_service.read_string(key, VECTOR_DEFAULTS.get(key, ""))


def is_vector_search_enabled() -> bool:
    return get_vector_setting("vector_search_enabled").lower() == "true"


def get_vector_provider() -> str:
    return normalize_provider(get_vector_setting("vector_provider"))


def get_vector_url() -> str:
    return get_vector_setting("vector_url")


def get_vector_model() -> str:
    return get_vector_setting("vector_model")


def get_vector_auth_type() -> str:
    return get_vector_setting("vector_auth_type")


def _get_stored_api_key_plaintext() -> Optional[str]:
    encrypted = system_settings_service.read_string("vector_api_key")
    if not encrypted:
        return None
    try:
        return decrypt_value(encrypted)
    except Exception:
        # After a key rotation the stored value may no longer decrypt. Sending
        # the ciphertext as the API key would only produce confusing auth
        # failures upstream; report it as missing instead.
        logger.warning("Stored vector API key could not be decrypted; re-enter it in Settings")
        return None


def save_vector_setting(key: str, value: str) -> bool:
    if key not in VECTOR_KEYS:
        raise ValueError(f"Unknown vector setting: {key}")
    system_settings_service.write_raw({key: value})
    return True


def save_vector_api_key(api_key: str) -> bool:
    if not api_key.strip():
        return clear_vector_api_key()
    encrypted = encrypt_value(api_key.strip())
    system_settings_service.write_raw({"vector_api_key": encrypted})
    return True


def clear_vector_api_key() -> bool:
    system_settings_service.delete(["vector_api_key"])
    return True


def save_vector_settings(
    enabled: Optional[bool] = None,
    provider: Optional[str] = None,
    url: Optional[str] = None,
    model: Optional[str] = None,
    dimension: Optional[int] = None,
    auth_type: Optional[str] = None,
    api_key: Optional[str] = None,
) -> dict:
    if enabled is not None:
        save_vector_setting("vector_search_enabled", "true" if enabled else "false")
    if provider is not None:
        provider = normalize_provider(provider)
        if provider not in VECTOR_PROVIDERS:
            raise ValueError("provider must be ollama or openai")
        save_vector_setting("vector_provider", provider)
    if url is not None:
        save_vector_setting("vector_url", url.strip().rstrip("/"))
    if model is not None:
        save_vector_setting("vector_model", model.strip())
    if dimension is not None:
        save_vector_setting("vector_dimension", str(int(dimension)))
    if auth_type is not None:
        if auth_type not in ("none", "bearer", "api_key"):
            raise ValueError("auth_type must be none, bearer, or api_key")
        save_vector_setting("vector_auth_type", auth_type)
    if api_key is not None:
        save_vector_api_key(api_key)
    try:
        from app.services.embedding_service import _reset_cache

        _reset_cache()
    except Exception:
        pass
    return get_vector_settings()


def test_vector_connection() -> dict:
    from app.services import embedding_service

    status = embedding_service.get_embedding_backend_status()
    if status.get("backend") == "disabled":
        return {
            "success": False,
            "error": "Vector search is disabled. Enable it first.",
        }
    if status.get("backend") == "unavailable":
        return {
            "success": False,
            "error": embedding_service.describe_error(
                status.get("error") or "exception:unreachable",
                status.get("provider", "ollama"),
                status.get("vector_url", ""),
            ),
        }
    if not status.get("model_configured"):
        return {
            "success": False,
            "error": f"Model '{status.get('model', 'unknown')}' not found on backend",
        }
    return {
        "success": True,
        "backend": status.get("backend"),
        "model": status.get("model"),
    }
