"""One place that answers: can we ask a model, and how?

Agent Core works without one. Several features are better with one — judging
whether a record is still worth keeping, deciding whether a newer memory
supersedes an older one — and those are judgements no amount of rules can make,
because they depend on meaning rather than shape.

So a model is a *capability*, never a dependency. Nothing here is required for
memory, search, credentials or connectors to work; features that can use a model
ask `is_available()` and fall back when the answer is no. Configuring one turns
those features on, including for records written long before it existed.

Two ways to reach one, because installations differ:

- ``ollama``: a URL and a model name, the same shape the embedding settings
  already use. Points at a machine you control, so nothing leaves it.
- ``binding``: an existing connector binding, which is how this system reaches
  every other external service — scoped, audited, and swappable.

Left unset, `provider` is empty and every dependent feature reports itself as
unconfigured rather than failing.
"""

import json
import logging
from typing import Optional

import httpx

from app.database import get_db

logger = logging.getLogger(__name__)

PROVIDERS = ("", "ollama", "binding")
DEFAULT_TIMEOUT_SECONDS = 60


def _setting(key: str, default: str = "") -> str:
    try:
        with get_db() as conn:
            row = conn.execute(
                "SELECT value FROM system_settings WHERE key = ?", (key,)
            ).fetchone()
        return (row["value"] if row else default) or default
    except Exception:
        return default


def get_config() -> dict:
    """How to reach a model, if one is configured at all."""
    provider = _setting("review_model_provider").strip().lower()
    if provider not in PROVIDERS:
        provider = ""
    config = {
        "provider": provider,
        "url": _setting("review_model_url").rstrip("/"),
        "model": _setting("review_model_name"),
        "binding_id": _setting("review_model_binding_id"),
        "action": _setting("review_model_action", "POST /chat/completions"),
        "timeout_seconds": int(
            _setting("review_model_timeout_seconds", str(DEFAULT_TIMEOUT_SECONDS))
            or DEFAULT_TIMEOUT_SECONDS
        ),
    }
    if provider == "ollama":
        config["configured"] = bool(config["url"] and config["model"])
    elif provider == "binding":
        config["configured"] = bool(config["binding_id"])
    else:
        config["configured"] = False
    return config


def is_available() -> bool:
    """Whether a model is configured. Callers must degrade when this is False."""
    return get_config()["configured"]


def describe_unavailable() -> str:
    """Why a model-backed feature cannot run, in terms an operator can act on."""
    config = get_config()
    if not config["provider"]:
        return (
            "No review model configured. Set one in Settings to enable the features "
            "that need judgement rather than rules; everything else works without it."
        )
    if config["provider"] == "ollama":
        return "The review model is set to a local endpoint but the URL or model name is missing."
    return "The review model is set to a connector binding but no binding is selected."


def _complete_ollama(config: dict, prompt: str) -> Optional[str]:
    try:
        response = httpx.post(
            f"{config['url']}/api/chat",
            json={
                "model": config["model"],
                "messages": [{"role": "user", "content": prompt}],
                "stream": False,
                "options": {"temperature": 0},
            },
            timeout=config["timeout_seconds"],
        )
        response.raise_for_status()
        payload = response.json()
    except Exception:
        logger.warning("Local review model call failed", exc_info=True)
        return None
    message = payload.get("message")
    if isinstance(message, dict):
        return str(message.get("content") or "")
    return str(payload.get("response") or "")


def _complete_binding(config: dict, prompt: str) -> Optional[str]:
    from app.services import connector_service
    from app.security.effective_authority import system_authority

    params = {"messages": [{"role": "user", "content": prompt}], "temperature": 0}
    if config["model"]:
        params["model"] = config["model"]
    try:
        result = connector_service.execute_authorized_binding_action_with_logging(
            config["binding_id"], config["action"], params,
            system_authority("configured local review model invocation"),
        )
    except Exception:
        logger.warning("Review model binding call failed", exc_info=True)
        return None
    if not (result.get("success") or result.get("ok")):
        logger.warning(
            "Review model binding returned an error: %s", str(result.get("error"))[:200]
        )
        return None
    return _reply_text(result)


def _reply_text(result: dict) -> str:
    """Pull assistant text out of whatever shape the provider returned.

    Deliberately tolerant: the binding could point at anything, and a reply that
    cannot be read is handled as "no answer" rather than as an error.
    """
    body = result.get("data") if isinstance(result.get("data"), (dict, list)) else result
    if isinstance(body, str):
        return body
    if not isinstance(body, dict):
        return ""
    choices = body.get("choices")
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        message = choices[0].get("message")
        if isinstance(message, dict) and message.get("content"):
            return str(message["content"])
        if choices[0].get("text"):
            return str(choices[0]["text"])
    message = body.get("message")
    if isinstance(message, dict) and message.get("content"):
        return str(message["content"])
    return str(body.get("response") or body.get("content") or "")


def complete(prompt: str) -> Optional[str]:
    """Ask the configured model. None when unavailable or the call failed.

    Never raises: a model is an optional aid, and a feature that uses one must
    degrade rather than break when it is missing or having a bad day.
    """
    config = get_config()
    if not config["configured"]:
        return None
    if config["provider"] == "ollama":
        return _complete_ollama(config, prompt)
    return _complete_binding(config, prompt)


def extract_json(text: Optional[str]) -> Optional[dict]:
    """Parse a JSON object out of a reply, tolerating prose around it.

    Returns None rather than guessing. A model that cannot state its answer has
    not given one, and features here act on deletions and supersessions.
    """
    if not text:
        return None
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        payload = json.loads(text[start : end + 1])
    except (TypeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def validate() -> dict:
    """Probe the configured model and report what happened.

    Configuration alone does not mean a model answers. Operators enable these
    features expecting them to work, so there is an explicit check rather than
    discovering it from an empty result days later.
    """
    config = get_config()
    if not config["configured"]:
        return {"ok": False, "provider": config["provider"], "error": describe_unavailable()}

    reply = complete(
        'Reply with this exact JSON and nothing else: {"ok": true}'
    )
    if reply is None:
        return {
            "ok": False,
            "provider": config["provider"],
            "error": "The model could not be reached. Check the URL, model name, or binding.",
        }
    parsed = extract_json(reply)
    if not parsed:
        return {
            "ok": False,
            "provider": config["provider"],
            "error": (
                "The model answered but not with usable JSON. Features here need a model "
                f"that can follow a format instruction. It said: {reply[:120]}"
            ),
        }
    return {
        "ok": True,
        "provider": config["provider"],
        "model": config["model"] or config["binding_id"],
        "detail": "The model answered and followed the requested format.",
    }
