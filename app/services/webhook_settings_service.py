"""Validated retry policy settings for the outbound webhook queue."""

from app.services import system_settings_service

DEFAULTS = {
    "webhook_retry_max_attempts": 5,
    "webhook_retry_initial_seconds": 1,
    "webhook_retry_max_seconds": 300,
    "webhook_retry_jitter_seconds": 1,
}


def retry_policy() -> dict[str, int]:
    values = {
        key: system_settings_service.read_int(key, default)
        for key, default in DEFAULTS.items()
    }
    values["webhook_retry_max_attempts"] = min(max(values["webhook_retry_max_attempts"], 1), 100)
    values["webhook_retry_initial_seconds"] = min(max(values["webhook_retry_initial_seconds"], 1), 3600)
    values["webhook_retry_max_seconds"] = min(max(values["webhook_retry_max_seconds"], values["webhook_retry_initial_seconds"]), 86400)
    values["webhook_retry_jitter_seconds"] = min(max(values["webhook_retry_jitter_seconds"], 0), values["webhook_retry_max_seconds"])
    return values
