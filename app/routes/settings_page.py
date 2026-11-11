"""Settings pages (general, password, OTP) and settings/prune/vector APIs."""

import asyncio
import httpx
from urllib.parse import urlparse

from fastapi import APIRouter, Request, Depends
from pydantic import BaseModel

from app.branding import CREDENTIAL_PREFIX, ENV_PREFIX
from app.security.dependencies import require_admin
from app.security.response_helpers import success_response, error_response
from app.services.auth_service import get_user_by_id
from app.routes.dashboard_shared import (
    render_page,
    require_auth,
    escape_html,
    local_dt,
    _parse_manual_prune_cutoff,
)

router = APIRouter()


SETTINGS_TABS_JS = """
<script>
(function() {
  var tabs = document.querySelectorAll('[data-tab]');
  var panels = document.querySelectorAll('[data-tab-panel]');
  if (!tabs.length) return;
  function show(key) {
    var found = false;
    panels.forEach(function(p) { if (p.dataset.tabPanel === key) found = true; });
    if (!found) key = panels[0].dataset.tabPanel;
    panels.forEach(function(p) { p.hidden = p.dataset.tabPanel !== key; });
    tabs.forEach(function(t) {
      var on = t.dataset.tab === key;
      t.classList.toggle('active', on);
      t.setAttribute('aria-selected', on ? 'true' : 'false');
    });
    try { history.replaceState(null, '', '#' + key); } catch (e) {}
  }
  tabs.forEach(function(t) { t.addEventListener('click', function() { show(t.dataset.tab); }); });
  show((location.hash || '').slice(1));
})();
</script>
"""

PRUNE_JS = """
<script>
async function pruneActivity() {
  var date = document.getElementById('prune-activity-before').value;
  if (!date) { showToast('Pick a cutoff date', 'warning'); return; }
  if (!confirm('Permanently delete finished activity older than ' + date + '?')) return;
  var j = await apiFetch('/api/dashboard/prune', { method: 'POST', body: JSON.stringify({ resource_type: 'activity', before_date: date }) });
  var box = document.getElementById('prune-activity-result');
  if (j.ok) { box.innerHTML = '<div class="alert alert-success">Deleted ' + escapeHtml(String(j.data.deleted_count)) + ' tasks older than ' + escapeHtml(date) + '.</div>'; }
  else { showToast((j.error && j.error.message) || 'Delete failed', 'danger'); }
}
</script>
"""


class ManualPruneRequest(BaseModel):
    resource_type: str
    before_date: str


@router.post("/api/dashboard/user-settings")
async def update_dashboard_user_settings(
    request: Request, session: dict = Depends(require_auth)
):
    from app.services import auth_service

    body = await request.json()
    timezone = body.get("timezone")
    if timezone is not None:
        timezone = str(timezone).strip() or None
    try:
        auth_service.update_user_timezone(session["user_id"], timezone)
    except ValueError as e:
        return error_response("INVALID_TIMEZONE", str(e), 400)
    return success_response({"timezone": timezone})


@router.post("/api/dashboard/system-settings")
async def update_dashboard_system_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services import audit_service, system_settings_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    settings_to_save = {}
    # Every field is optional: a form that owns only some settings (the Webhooks
    # retry policy) sends only those.
    if "scratchpad_retention_days" in body:
        retention_raw = str(body.get("scratchpad_retention_days", "")).strip()
        try:
            retention_days = int(retention_raw)
        except ValueError:
            return error_response(
                "INVALID_RETENTION",
                "Scratchpad retention must be a whole number of days",
                400,
            )
        if retention_days < 1 or retention_days > 365:
            return error_response(
                "INVALID_RETENTION",
                "Scratchpad retention must be between 1 and 365 days",
                400,
            )
        settings_to_save["scratchpad_retention_days"] = str(retention_days)

    # Optional (backward compatible): only validate/save if the caller sent it,
    # so existing callers that don't yet know about this field are unaffected.
    retracted_retention_days = None
    if "retracted_retention_days" in body:
        retracted_retention_raw = str(body.get("retracted_retention_days", "")).strip()
        try:
            retracted_retention_days = int(retracted_retention_raw)
        except ValueError:
            return error_response(
                "INVALID_RETRACTED_RETENTION",
                "Retracted/superseded retention must be a whole number of days",
                400,
            )
        if retracted_retention_days < 1 or retracted_retention_days > 365:
            return error_response(
                "INVALID_RETRACTED_RETENTION",
                "Retracted/superseded retention must be between 1 and 365 days",
                400,
            )

    if retracted_retention_days is not None:
        settings_to_save["retracted_retention_days"] = str(retracted_retention_days)

    # Optional (backward compatible), same as retracted_retention_days above.
    if "episodic_memory_ttl_days" in body:
        try:
            episodic_ttl = int(str(body.get("episodic_memory_ttl_days", "")).strip())
        except ValueError:
            return error_response(
                "INVALID_EPISODIC_TTL",
                "Episodic memory expiry must be a whole number of days",
                400,
            )
        # 0 is meaningful: store episodic writes permanently, like any other record.
        if episodic_ttl < 0 or episodic_ttl > 365:
            return error_response(
                "INVALID_EPISODIC_TTL",
                "Episodic memory expiry must be between 0 and 365 days",
                400,
            )
        settings_to_save["episodic_memory_ttl_days"] = str(episodic_ttl)

    if "unconfirmed_inference_days" in body:
        try:
            ui_days = int(str(body.get("unconfirmed_inference_days", "")).strip())
        except ValueError:
            return error_response(
                "INVALID_UNCONFIRMED_INFERENCE_DAYS",
                "Unconfirmed inference cutoff must be a whole number of days",
                400,
            )
        if ui_days < 1 or ui_days > 90:
            return error_response(
                "INVALID_UNCONFIRMED_INFERENCE_DAYS",
                "Unconfirmed inference cutoff must be between 1 and 90 days",
                400,
            )
        settings_to_save["unconfirmed_inference_days"] = str(ui_days)

    if "unconfirmed_inference_min_importance" in body:
        try:
            ui_min_importance = float(
                str(body.get("unconfirmed_inference_min_importance", "")).strip()
            )
        except ValueError:
            return error_response(
                "INVALID_UNCONFIRMED_INFERENCE_MIN_IMPORTANCE",
                "Unconfirmed inference minimum importance must be a number",
                400,
            )
        if not 0.0 <= ui_min_importance <= 1.0:
            return error_response(
                "INVALID_UNCONFIRMED_INFERENCE_MIN_IMPORTANCE",
                "Unconfirmed inference minimum importance must be between 0.0 and 1.0",
                400,
            )
        settings_to_save["unconfirmed_inference_min_importance"] = str(
            ui_min_importance
        )

    for key, minimum, maximum in (
        ("proposal_pending_cap_per_rule", 1, 1000),
        ("proposal_pending_cap_total", 1, 2000),
        ("proposal_generation_budget_per_run", 1, 500),
    ):
        if key not in body:
            continue
        try:
            value = int(str(body[key]).strip())
        except ValueError:
            return error_response(
                "INVALID_PROPOSAL_CAP",
                f"{key} must be a whole number",
                400,
            )
        if not minimum <= value <= maximum:
            return error_response(
                "INVALID_PROPOSAL_CAP",
                f"{key} must be between {minimum} and {maximum}",
                400,
            )
        settings_to_save[key] = str(value)

    if "consolidation_scan_enabled" in body:
        raw = str(body.get("consolidation_scan_enabled", "")).strip().lower()
        settings_to_save["consolidation_scan_enabled"] = (
            "1" if raw in ("true", "1") else "0"
        )

    for key, label in (
        ("execution_log_retention_days", "Connector execution log retention"),
        ("webhook_log_retention_days", "Webhook delivery log retention"),
    ):
        if key in body:
            try:
                days = int(str(body.get(key, "")).strip())
            except ValueError:
                return error_response(
                    "INVALID_LOG_RETENTION",
                    f"{label} must be a whole number of days",
                    400,
                )
            # 0 is meaningful: keep the log forever, which is the pre-retention
            # behaviour and the reason a 180 MB database was 142 MB of log.
            if days < 0 or days > 365:
                return error_response(
                    "INVALID_LOG_RETENTION",
                    f"{label} must be between 0 and 365 days",
                    400,
                )
            settings_to_save[key] = str(days)

    if "memory_dedupe_similarity" in body:
        try:
            dedupe_similarity = float(
                str(body.get("memory_dedupe_similarity", "")).strip()
            )
        except ValueError:
            return error_response(
                "INVALID_DEDUPE_SIMILARITY",
                "Duplicate warning threshold must be a number",
                400,
            )
        if not 0.5 <= dedupe_similarity <= 1.0:
            return error_response(
                "INVALID_DEDUPE_SIMILARITY",
                "Duplicate warning threshold must be between 0.5 and 1.0",
                400,
            )
        settings_to_save["memory_dedupe_similarity"] = str(dedupe_similarity)

    for key, label, minimum, maximum in (
        ("webhook_retry_max_attempts", "Webhook retry attempts", 1, 100),
        ("webhook_retry_initial_seconds", "Webhook initial retry delay", 1, 3600),
        ("webhook_retry_max_seconds", "Webhook maximum retry delay", 1, 86400),
        ("webhook_retry_jitter_seconds", "Webhook retry jitter", 0, 86400),
    ):
        if key not in body:
            continue
        try:
            value = int(str(body[key]).strip())
        except ValueError:
            return error_response(
                "INVALID_WEBHOOK_RETRY", f"{label} must be a whole number", 400
            )
        if not minimum <= value <= maximum:
            return error_response(
                "INVALID_WEBHOOK_RETRY",
                f"{label} must be between {minimum} and {maximum}",
                400,
            )
        settings_to_save[key] = str(value)

    if (
        "webhook_retry_initial_seconds" in settings_to_save
        and "webhook_retry_max_seconds" in settings_to_save
        and int(settings_to_save["webhook_retry_max_seconds"])
        < int(settings_to_save["webhook_retry_initial_seconds"])
    ):
        return error_response(
            "INVALID_WEBHOOK_RETRY",
            "Webhook maximum retry delay must not be lower than the initial delay",
            400,
        )
    system_settings_service.write_raw(settings_to_save)

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="system_settings",
        result="success",
        details=settings_to_save,
        ip_address=get_client_ip(request),
    )
    return success_response({"settings": settings_to_save})


@router.post("/api/dashboard/prune")
def prune_dashboard_data(
    body: ManualPruneRequest,
    request: Request,
    session: dict = Depends(require_admin),
):
    from app.database import get_db
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    resource_type = body.resource_type.strip().lower()
    if resource_type not in {"audit", "activity"}:
        return error_response(
            "INVALID_RESOURCE",
            "resource_type must be either 'audit' or 'activity'",
            400,
        )

    try:
        cutoff_iso, cutoff_sql = _parse_manual_prune_cutoff(body.before_date)
    except ValueError:
        return error_response(
            "INVALID_DATE",
            "before_date must be a valid ISO date or datetime",
            400,
        )

    deleted = 0
    with get_db() as conn:
        if resource_type == "audit":
            cursor = conn.execute(
                "DELETE FROM audit_log WHERE timestamp < ?",
                (cutoff_sql,),
            )
            deleted = cursor.rowcount
        else:
            cursor = conn.execute(
                """
                DELETE FROM agent_activity
                WHERE status IN ('completed', 'cancelled', 'blocked')
                  AND COALESCE(ended_at, updated_at, started_at) < ?
                """,
                (cutoff_iso,),
            )
            deleted = cursor.rowcount
        conn.commit()

    action = "audit_pruned" if resource_type == "audit" else "activity_pruned"
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action=action,
        resource_type=resource_type,
        result="success",
        details={
            "before_date": body.before_date,
            "cutoff": cutoff_iso,
            "deleted_count": deleted,
        },
        ip_address=get_client_ip(request),
    )
    return success_response(
        {
            "resource_type": resource_type,
            "before_date": body.before_date,
            "deleted_count": deleted,
        }
    )


@router.post("/api/dashboard/vector-settings")
async def update_dashboard_vector_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services.vector_settings_service import save_vector_settings
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    body = await request.json()
    enabled_raw = body.get("vector_search_enabled")
    provider = body.get("vector_provider", "ollama").strip()
    auth_type = body.get("vector_auth_type", "none").strip()
    api_key = body.get("vector_api_key")
    model = body.get("vector_model", "").strip()
    url = body.get("vector_url", "").strip()
    dimension_raw = body.get("vector_dimension")
    query_prefix = body.get("vector_query_prefix")
    if query_prefix is not None and (not isinstance(query_prefix, str) or len(query_prefix) > 100):
        return error_response(
            "INVALID_QUERY_PREFIX", "vector_query_prefix must be text of at most 100 characters", 400
        )

    if enabled_raw is not None and enabled_raw not in ("true", "false"):
        return error_response(
            "INVALID_ENABLED", "vector_search_enabled must be true or false", 400
        )

    from app.services.vector_settings_service import VECTOR_PROVIDERS, normalize_provider

    provider = normalize_provider(provider)
    if provider not in VECTOR_PROVIDERS:
        return error_response(
            "INVALID_PROVIDER", "vector_provider must be ollama or openai", 400
        )

    if auth_type not in ("none", "bearer", "api_key"):
        return error_response(
            "INVALID_AUTH_TYPE",
            "vector_auth_type must be none, bearer, or api_key",
            400,
        )

    if not model:
        return error_response("INVALID_MODEL", "vector_model is required", 400)

    if not url:
        return error_response("INVALID_URL", "vector_url is required", 400)

    try:
        dimension = int(dimension_raw) if dimension_raw else None
        if dimension is not None and (dimension < 64 or dimension > 4096):
            return error_response(
                "INVALID_DIMENSION", "vector_dimension must be between 64 and 4096", 400
            )
    except ValueError:
        return error_response(
            "INVALID_DIMENSION", "vector_dimension must be a number", 400
        )

    updated = save_vector_settings(
        enabled=None if enabled_raw is None else (enabled_raw == "true"),
        provider=provider,
        url=url,
        model=model,
        dimension=dimension,
        auth_type=auth_type,
        api_key=api_key,
        query_prefix=query_prefix,
    )

    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="vector_settings",
        result="success",
        details={
            "vector_search_enabled": updated["vector_search_enabled"],
            "vector_auth_type": updated["vector_auth_type"],
            "vector_model": updated["vector_model"],
        },
        ip_address=get_client_ip(request),
    )

    return success_response({"settings": updated})


@router.post("/api/dashboard/vector-settings/test")
async def test_dashboard_vector_settings(
    request: Request, session: dict = Depends(require_admin)
):
    from app.services.vector_settings_service import test_vector_connection
    from app.services import audit_service
    from app.routes.auth import get_client_ip

    result = await asyncio.to_thread(test_vector_connection)
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="setup_verification",
        resource_type="vector_settings",
        result="success" if result.get("success") else "failure",
        details={k: v for k, v in result.items() if k != "error"},
        ip_address=get_client_ip(request),
    )
    if result.get("success"):
        return success_response(result)
    return error_response(
        "VECTOR_TEST_FAILED", result.get("error", "Vector test failed"), 400
    )


@router.get("/api/dashboard/vector-reembed")
async def vector_reembed_status(session: dict = Depends(require_admin)):
    from app.services import reembed_service

    return success_response(await asyncio.to_thread(reembed_service.status))


@router.post("/api/dashboard/vector-reembed")
async def start_vector_reembed(
    request: Request, session: dict = Depends(require_admin)
):
    """Embed every memory record that has no vector for the configured model.

    Optional JSON body: {"pause_ms": N} pauses after each record (up to 30000),
    so a run can share a slow embedding backend with its other clients.
    """
    from app.services import audit_service, reembed_service
    from app.routes.auth import get_client_ip

    pause_ms = 0
    try:
        body = await request.json()
    except Exception:
        body = None
    if isinstance(body, dict) and body.get("pause_ms") is not None:
        try:
            pause_ms = int(body["pause_ms"])
        except (TypeError, ValueError):
            return error_response("INVALID_PAUSE", "pause_ms must be a number of milliseconds", 400)
        if not 0 <= pause_ms <= reembed_service.MAX_PAUSE_MS:
            return error_response(
                "INVALID_PAUSE", f"pause_ms must be between 0 and {reembed_service.MAX_PAUSE_MS}", 400
            )

    state, err = await asyncio.to_thread(
        reembed_service.start, f"user:{session['user_id']}", pause_ms
    )
    if err == "VECTOR_SEARCH_DISABLED":
        return error_response(err, "Enable vector search and save the settings first.", 400)
    if err == "REEMBED_ALREADY_RUNNING":
        return error_response(err, "A re-embed run is already in progress.", 409)
    audit_service.write_event(
        actor_type="user",
        actor_id=session["user_id"],
        action="system_setting_updated",
        resource_type="vector_settings",
        result="success",
        details={"operation": "reembed_started", "model": state["model"], "run_id": state["run_id"]},
        ip_address=get_client_ip(request),
    )
    return success_response({"job": state})


@router.post("/api/dashboard/vector-reembed/estimate")
async def estimate_vector_reembed(request: Request, session: dict = Depends(require_admin)):
    """Time a synthetic, average-length sample on a model and project the re-embed and write cost.

    Optional JSON body {"model": ..., "url": ...} measures a model before it is
    saved; both default to the saved settings. Nothing is stored.
    """
    from app.services import reembed_service

    try:
        body = await request.json()
    except Exception:
        body = None
    body = body if isinstance(body, dict) else {}
    model = body.get("model")
    url = body.get("url")
    if model is not None and (not isinstance(model, str) or not model.strip()):
        return error_response("INVALID_MODEL", "model must be a non-empty string", 400)
    if url is not None:
        parsed = urlparse(str(url).strip())
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return error_response("INVALID_URL", "url must be an http(s) URL", 400)
    provider, auth_type, api_key, err = _form_endpoint_auth(body)
    if err:
        return err
    result = await asyncio.to_thread(
        reembed_service.estimate, model, url, provider, auth_type, api_key
    )
    if not result.get("ok"):
        return error_response("ESTIMATE_FAILED", result.get("error", "Could not measure the model"), 502)
    return success_response(result)


@router.post("/api/dashboard/vector-reembed/cancel")
async def cancel_vector_reembed(session: dict = Depends(require_admin)):
    from app.services import reembed_service

    if not await asyncio.to_thread(reembed_service.cancel):
        return error_response("REEMBED_NOT_RUNNING", "No re-embed run is in progress.", 409)
    return success_response({"cancelling": True})


def _form_endpoint_auth(body: dict):
    """Provider and a typed key from a settings-form request: (provider, auth_type, api_key, error)."""
    from app.services.vector_settings_service import VECTOR_PROVIDERS, normalize_provider

    provider = body.get("provider")
    if provider is not None:
        provider = normalize_provider(str(provider))
        if provider not in VECTOR_PROVIDERS:
            return None, None, None, error_response(
                "INVALID_PROVIDER", "provider must be ollama or openai", 400
            )
    auth_type = body.get("auth_type")
    if auth_type is not None and auth_type not in ("none", "bearer", "api_key"):
        return None, None, None, error_response(
            "INVALID_AUTH_TYPE", "auth_type must be none, bearer, or api_key", 400
        )
    api_key = body.get("api_key") or None
    if api_key is not None and not isinstance(api_key, str):
        return None, None, None, error_response("INVALID_API_KEY", "api_key must be a string", 400)
    return provider, auth_type, api_key, None


@router.post("/api/dashboard/vector-models")
async def list_vector_models(request: Request, session: dict = Depends(require_admin)):
    """List what an embedding endpoint reports, for the settings form.

    Body: {"provider": "ollama"|"openai", "url": ..., "auth_type"?, "api_key"?}.
    A key typed into the form is sent to that URL; otherwise the saved key is
    used only when the URL is the saved one.
    """
    from app.services import embedding_service
    from app.services.vector_settings_service import get_vector_provider

    try:
        body = await request.json()
    except Exception:
        body = None
    body = body if isinstance(body, dict) else {}
    url = str(body.get("url") or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return error_response("INVALID_URL", "url must be an http(s) URL", 400)
    provider, auth_type, api_key, err = _form_endpoint_auth(body)
    if err:
        return err
    provider = provider or get_vector_provider()
    headers = embedding_service.headers_for_endpoint(url, auth_type, api_key)
    try:
        models = await asyncio.to_thread(embedding_service.list_models, provider, url, headers)
    except embedding_service.EmbeddingRequestError as e:
        return error_response("MODEL_LIST_FAILED", embedding_service.describe_error(e.code, provider, url, e.detail), 502)
    except Exception as e:
        return error_response(
            "MODEL_LIST_FAILED",
            embedding_service.describe_error(f"exception:{type(e).__name__}", provider, url),
            502,
        )
    return success_response({"models": models, "provider": provider})


@router.get("/api/dashboard/ollama-models")
async def list_ollama_models(url: str, session: dict = Depends(require_admin)):
    """List what an Ollama-compatible endpoint has installed.

    Filtering by kind is deliberately not attempted — the tag list does not reliably say
    which models are embedding models, and guessing would hide a usable one.
    """
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return error_response("INVALID_URL", "vector_url must be an http(s) URL", 400)

    tags_url = url.strip().rstrip("/") + "/api/tags"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(tags_url)
        if response.status_code != 200:
            return error_response(
                "MODEL_LIST_FAILED",
                f"Ollama returned {response.status_code} while listing models",
                502,
            )
        data = response.json()
    except Exception:
        return error_response(
            "MODEL_LIST_FAILED", "Could not fetch models from Ollama", 502
        )

    models = [
        model.get("name")
        for model in data.get("models", [])
        if isinstance(model, dict) and model.get("name")
    ]
    return success_response({"models": models})


@router.get("/settings")
def settings_page(request: Request, session: dict = Depends(require_auth)):
    from app.database import get_db

    user = get_user_by_id(session["user_id"])
    is_admin = session.get("role") == "admin"
    defaults = {
        "scratchpad_retention_days": "7",
        "retracted_retention_days": "30",
        "episodic_memory_ttl_days": "30",
        "execution_log_retention_days": "30",
        "webhook_log_retention_days": "30",
        "memory_dedupe_similarity": "0.92",
        "unconfirmed_inference_days": "90",
        "unconfirmed_inference_min_importance": "0.7",
        "consolidation_scan_enabled": "1",
    }
    with get_db() as conn:
        placeholders = ",".join("?" for _ in defaults)
        rows = conn.execute(
            f"SELECT key, value FROM system_settings WHERE key IN ({placeholders})",
            tuple(defaults),
        ).fetchall()
        stored_settings = {row["key"]: row["value"] for row in rows}
        count_row = conn.execute("SELECT COUNT(*) AS count FROM credentials").fetchone()

    values = defaults | stored_settings
    scratchpad_retention_days = values["scratchpad_retention_days"]
    retracted_retention_days = values["retracted_retention_days"]
    episodic_memory_ttl_days = values["episodic_memory_ttl_days"]
    execution_log_retention_days = values["execution_log_retention_days"]
    webhook_log_retention_days = values["webhook_log_retention_days"]
    memory_dedupe_similarity = values["memory_dedupe_similarity"]
    unconfirmed_inference_days = values["unconfirmed_inference_days"]
    unconfirmed_inference_min_importance = values["unconfirmed_inference_min_importance"]
    consolidation_scan_enabled = values["consolidation_scan_enabled"] in ("1", "true")
    credential_count = int(count_row["count"] if count_row else 0)

    account_html = f"""
    <div class="two-col">
      <div class="card">
        <h2>Account</h2>
        <p><strong>Display Name:</strong> {escape_html(user.get("display_name") or "")}</p>
        <p><strong>Email:</strong> {escape_html(user.get("email") or "")}</p>
        <p><strong>Role:</strong> {escape_html(session.get("role") or "user")}</p>
      </div>
      <div class="card">
        <h2>Sign-in</h2>
        <div class="form-group">
          <a href="/settings/password" class="btn btn-secondary">Change Password</a>
        </div>
        <div class="form-group">
          <a href="/settings/otp" class="btn btn-secondary">Manage two-step sign-in</a>
        </div>
      </div>
    </div>"""

    from zoneinfo import available_timezones

    current_tz = user.get("timezone") or ""
    tz_option_list = '<option value="">Auto-detect from browser</option>'
    for _zone in sorted(available_timezones()):
        _sel = " selected" if _zone == current_tz else ""
        tz_option_list += (
            f'<option value="{escape_html(_zone)}"{_sel}>{escape_html(_zone)}</option>'
        )
    preferences_html = f"""
    <div class="card">
      <div class="section-header">
        <h2>Preferences</h2>
        <div class="section-note">Personal display options for your account.</div>
      </div>
      <form id="user-settings-form" onsubmit="saveUserSettings(event)">
        <div class="form-group">
          <label for="user-timezone">Timezone</label>
          <select id="user-timezone" style="max-width:360px">{tz_option_list}</select>
          <p class="form-hint">Dates and times across the dashboard are shown in this timezone. Stored data stays in UTC.</p>
        </div>
        <button type="submit" class="btn">Save Preferences</button>
        <div id="user-settings-result" style="margin-top:12px"></div>
      </form>
    </div>
    <div class="card">
      <div class="section-header">
        <h2>Operator Tokens</h2>
        <div class="section-note">Short-lived keys for scripts that act as you, such as an attended install script. Requests made with one are recorded as yours. Agents use their own agent keys, not these.</div>
      </div>
      <div class="form-group">
        <label for="op-token-label">Label</label>
        <input type="text" id="op-token-label" maxlength="100" placeholder="what it is for" style="max-width:360px">
        <p class="form-hint">A reminder of what the token is for, shown in the list.</p>
      </div>
      <div class="form-group">
        <label for="op-token-minutes">Valid for</label>
        <select id="op-token-minutes" style="max-width:200px">
          <option value="15">15 minutes</option>
          <option value="30" selected>30 minutes</option>
          <option value="60">60 minutes</option>
        </select>
        <p class="form-hint">After this long the token stops working on its own.</p>
      </div>
      <button type="button" class="btn" onclick="createOperatorToken()">Create Token</button>
      <div id="op-token-result" style="margin-top:12px"></div>
      <div id="op-token-list" style="margin-top:12px"></div>
    </div>"""

    secrets_snapshot_html = ""
    if is_admin:
        from app.services.broker_service import get_broker_credential_hash
        from app.services.credential_rotation_service import get_key_status

        key_status = get_key_status()
        broker_active = bool(get_broker_credential_hash())
        broker_badge = "active" if broker_active else "stale"
        snapshot_rows = "".join(
            [
                f"<tr><td>Credential Entries</td><td>{credential_count}</td><td class='text-muted'>Secrets stored encrypted</td></tr>",
                f"<tr><td>Encryption Key</td><td><span class='badge badge-{'active' if key_status.get('mode') == 'keyring' else 'stale'}'>{'in use' if key_status.get('mode') == 'keyring' else escape_html(key_status.get('mode', 'unknown'))}</span></td><td class='text-muted'>{escape_html(key_status.get('keyring_size', 0))} {'key' if key_status.get('keyring_size', 0) == 1 else 'keys'} in use <details class='access-details' style='display:inline-block;margin-left:6px'><summary>Key ID</summary>Primary key ID: {escape_html(key_status.get('primary_key_id', 'none'))}</details></td></tr>",
                f"<tr><td>Local credential helper</td><td><span class='badge badge-{broker_badge}'>{'ready' if broker_active else 'missing'}</span></td><td class='text-muted'>Resolves {CREDENTIAL_PREFIX}* references at runtime</td></tr>",
            ]
        )
        secrets_snapshot_html = f"""
    <div class="card">
      <div class="section-header">
        <h2>Secrets Snapshot</h2>
        <div class="section-note">Current status for key and secret operations.</div>
      </div>
      <table>
        <thead><tr><th>Item</th><th>Status</th><th>Notes</th></tr></thead>
        <tbody>{snapshot_rows}</tbody>
      </table>
    </div>"""

    backup_html = ""
    if is_admin:
        from app.services.backup_service import get_maintenance_status

        maint_status = get_maintenance_status()
        if maint_status["scheduler_enabled"]:
            schedule_line = (
                f"Runs automatically every {maint_status['interval_minutes']} minutes."
            )
        else:
            schedule_line = (
                f"Automatic scheduling is disabled (<code>{ENV_PREFIX}MAINTENANCE_INTERVAL_MINUTES=0</code>); "
                "use the button below to run it manually."
            )
        if maint_status["last_run_at"]:
            summary = maint_status["last_run_summary"] or {}
            by = maint_status["last_run_by"] or "unknown"
            def _count(n, one, many):
                return f"{n} {one if n == 1 else many}"

            parts = []
            for key, one, many in (
                ("stale_activities_marked", "idle task marked", "idle tasks marked"),
                ("scratchpad_pruned", "temporary note deleted", "temporary notes deleted"),
                ("ttl_swept", "expired memory removed", "expired memories removed"),
                ("retracted_purged", "old retracted memory deleted", "old retracted memories deleted"),
                ("proposals_generated", "suggestion made", "suggestions made"),
            ):
                n = summary.get(key, 0) or 0
                if n:
                    parts.append(_count(n, one, many))
            last_run_line = (
                f"Last run {local_dt(maint_status['last_run_at'])} ({escape_html(by)}): "
                + (", ".join(parts) if parts else "nothing to clean up")
                + "."
            )
        else:
            last_run_line = "Last run: never. It runs by itself on the schedule above, or you can run it now."
        backup_html = f"""
    <div class="card">
      <div class="section-header">
        <h2>Backup & Restore</h2>
      </div>
      <div class="form-group">
        <button class="btn" onclick="exportBackup()">Export Encrypted Backup</button>
        <button class="btn btn-secondary" onclick="openModal('restore-modal')">Restore from Backup</button>
      </div>
      <div id="backup-result" style="margin-top:12px"></div>
      <hr class=divider>
      <h3 class="sub-heading section-title">Maintenance</h3>
      <div class="form-group">
        <button class="btn btn-secondary" onclick="runMaintenance()">Run Maintenance</button>
      </div>
      <p class="form-hint">Maintenance marks tasks stale after the idle limit, deletes temporary scratchpad notes and permanently deletes retracted or replaced memories past the retention limits above. It does not touch credentials or service bindings. {schedule_line}</p>
      <p class="form-hint" id="maintenance-last-run">{last_run_line}</p>
      <hr class=divider>
      <h3 class="sub-heading section-title">Startup Checks</h3>
      <p class="form-hint">Checks that the data folder is writable, text search is available, and the encryption key and local credential helper are in place. Run it after an upgrade or a restore.</p>
      <div id="startup-checks"><button class="btn btn-secondary btn-sm" onclick="runStartupChecks()">Run Checks</button></div>
    </div>"""

    encryption_key_html = ""
    if is_admin:
        encryption_key_html = """
    <div class="card">
      <h2>Encryption Key</h2>
      <p class="text-muted" style="margin-bottom:12px">This key protects stored credential values. Rotate it if you want to re-encrypt all saved secrets with a new primary key, or restore a known-good key if you need to recover from a bad rotation.</p>
      <div class="form-group">
        <button class="btn btn-secondary" onclick="loadCredentialKeyStatus()">Check Status</button>
        <button class="btn btn-warning" onclick="openModal('credential-key-rotate-modal')">Rotate Key</button>
        <button class="btn btn-secondary" onclick="openModal('credential-key-restore-modal')">Restore Key</button>
      </div>
      <ul class="form-hint" style="margin:0 0 0 18px">
        <li><strong>Check Status</strong>: shows how many encryption keys are in use.</li>
        <li><strong>Rotate Key</strong>: makes a new key and re-encrypts every stored secret with it. Use it after a suspected leak.</li>
        <li><strong>Restore Key</strong>: puts back an earlier key, for example after a failed rotation. It must be able to decrypt every stored secret.</li>
      </ul>
      <div id="credential-key-result" style="margin-top:12px"></div>
    </div>"""

    network_limits_html = ""
    if is_admin:
        network_limits_html = """
    <div class="card" id="network-limits">
      <h2>Network limits</h2>
      <p class="text-muted" style="margin-bottom:12px">Two limits you can turn up in steps. Start with <strong>report only</strong>: it lists what would be refused and refuses nothing, so you can allow what you expect before you enforce.</p>
      <h3 class="sub-heading section-title">Private addresses</h3>
      <div class="form-group">
        <label for="nl-internal-mode">Services, webhooks and imports reaching private addresses (like 192.168.x.x)</label>
        <select id="nl-internal-mode">
          <option value="allow">Allow</option>
          <option value="report">Report only</option>
          <option value="block">Block</option>
        </select>
        <p class="form-hint">Block stops anything that points at a private address unless it is in the allowed list below. A new install starts on Block; an install that was already running starts on Allow.</p>
        <p class="form-hint" id="nl-internal-env" style="display:none">This is set by an environment variable on the server; change it there.</p>
      </div>
      <div class="form-group">
        <label for="nl-allowed-internal">Allowed private addresses</label>
        <textarea id="nl-allowed-internal" rows="3" placeholder="192.168.1.0/24&#10;searxng&#10;media-box.lan"></textarea>
        <p class="form-hint">One per line: a host name, an address, or a range such as 192.168.1.0/24. These are always allowed, even when private addresses are blocked.</p>
      </div>
      <div id="nl-would-block"></div>
      <div class="actions-cell"><button type="button" class="btn" id="nl-save-internal">Save</button></div>
      <div id="nl-internal-note" class="alert" style="display:none;margin-top:8px"></div>
      <hr class="divider">
      <h3 class="sub-heading section-title">Local credential helper callers</h3>
      <p class="text-muted" id="nl-broker-status"></p>
      <div class="form-group">
        <label for="nl-broker-allowed">Allowed callers</label>
        <textarea id="nl-broker-allowed" rows="3" placeholder="192.168.1.20&#10;10.0.0.0/24"></textarea>
        <p class="form-hint">One address or range per line. This server itself is always allowed. Leave it empty to accept any caller that holds the helper key.</p>
      </div>
      <div class="form-group">
        <label for="nl-broker-mode">When a caller is not in the list</label>
        <select id="nl-broker-mode">
          <option value="audit">Report only (let it through, and record it)</option>
          <option value="enforce">Refuse it</option>
        </select>
        <p class="form-hint" id="nl-broker-env" style="display:none">This is set by an environment variable on the server; change it there.</p>
      </div>
      <div class="actions-cell"><button type="button" class="btn" id="nl-save-broker">Save</button></div>
      <div id="nl-broker-note" class="alert" style="display:none;margin-top:8px"></div>
      <script src="/static/js/network_limits.js?v=1"></script>
    </div>"""

    broker_html = ""
    if is_admin:
        broker_html = f"""
    <div class="card">
      <h2>Local credential helper</h2>
      <p class="text-muted" style="margin-bottom:12px">The local credential helper swaps <code>{CREDENTIAL_PREFIX}*</code> references for the real value when a command runs. Rotate its key if a local program should stop using the current one.</p>
        <button class="btn btn-warning" onclick="openModal('broker-rotate-modal')">Rotate helper key</button>
      <div id="broker-result" style="margin-top:12px"></div>
    </div>"""

    def _number(field_id, value, lo, hi, step=None):
        step_attr = f' step="{step}"' if step else ""
        return f'<input type="number" id="{field_id}" min="{lo}" max="{hi}"{step_attr} value="{escape_html(value)}" style="width:120px">'

    memory_behavior_html = ""
    retention_html = ""
    if is_admin:
        memory_behavior_html = f"""
    <div class="card">
      <h2>Memory behavior</h2>
      <form onsubmit="saveSystemSettings(event)">
        <div class="form-group">
          <label>Expire work-log memories after (days)</label>
          {_number("episodic-memory-ttl-days", episodic_memory_ttl_days, 0, 365)}
          <p class="form-hint">Notes that read like a work log (heartbeats, routine runs, ticket closeouts) expire after this many days instead of living forever. 0 keeps them permanently.</p>
        </div>
        <div class="form-group">
          <label>Warn about near-duplicate memories above</label>
          {_number("memory-dedupe-similarity", memory_dedupe_similarity, 0.5, 1, 0.01)}
          <p class="form-hint">A new memory this similar to an existing one in the same scope is flagged as a possible duplicate, never blocked. Needs vector search. Lower catches more and raises more false alarms.</p>
        </div>
        <h3 class="sub-heading section-title">Memory review</h3>
        <p class="form-hint">Agent Core asks you to check agent-written facts nobody has confirmed. These settings decide which ones it asks about. The questions appear under Memory Clean-up on the Memory page.</p>
        <div class="form-group">
          <label>Ask about unconfirmed facts older than (days)</label>
          {_number("unconfirmed-inference-days", unconfirmed_inference_days, 1, 90)}
          <p class="form-hint">1 to 90. Facts you have already confirmed with evidence are never asked about.</p>
        </div>
        <div class="form-group">
          <label>Only ask about facts at least this important</label>
          {_number("unconfirmed-inference-min-importance", unconfirmed_inference_min_importance, 0, 1, 0.05)}
          <p class="form-hint">0 to 1. Less important unconfirmed facts quietly drop in search ranking instead of asking you. Raise it for fewer questions.</p>
        </div>
        <label class="checkbox-label">
          <input type="checkbox" id="consolidation-scan-enabled" {"checked" if consolidation_scan_enabled else ""}>
          Look for suggestions automatically on the maintenance schedule
        </label>
        <p class="form-hint">Off means suggestions only appear when you press Check for more on the Memory page.</p>
        <button type="submit" class="btn">Save</button>
        <div class="save-result" style="margin-top:12px"></div>
      </form>
    </div>"""

        retention_html = f"""
    <div class="card">
      <h2>How long things are kept</h2>
      <p class="form-hint" style="margin-top:0;margin-bottom:14px">Run Maintenance (below) deletes anything older than these limits. Nothing here touches credentials or service bindings.</p>
      <form onsubmit="saveSystemSettings(event)">
        <div class="form-group">
          <label>Scratchpad notes (days)</label>
          {_number("scratchpad-retention-days", scratchpad_retention_days, 1, 365)}
          <p class="form-hint">Temporary notes agents leave for themselves.</p>
        </div>
        <div class="form-group">
          <label>Retracted and replaced memories (days)</label>
          {_number("retracted-retention-days", retracted_retention_days, 1, 365)}
          <p class="form-hint">Counted from when the memory stopped being active, so you have time to notice and restore a mistaken retraction.</p>
        </div>
        <div class="form-group">
          <label>Service call log (days)</label>
          {_number("execution-log-retention-days", execution_log_retention_days, 0, 365)}
          <p class="form-hint">Records of calls agents made through Services. Diagnostics only. 0 keeps them forever.</p>
        </div>
        <div class="form-group">
          <label>Webhook delivery log (days)</label>
          {_number("webhook-log-retention-days", webhook_log_retention_days, 0, 365)}
          <p class="form-hint">Records of notifications sent to other systems. 0 keeps them forever. Retry settings are on the Webhooks page.</p>
        </div>
        <button type="submit" class="btn">Save</button>
        <div class="save-result" style="margin-top:12px"></div>
      </form>
    </div>"""

    vector_settings_html = ""
    if is_admin:
        from app.services.vector_settings_service import get_vector_settings

        vector_settings = get_vector_settings()
        vector_enabled = vector_settings["vector_search_enabled"].lower() == "true"
        vector_provider = vector_settings.get("vector_provider", "ollama")
        vector_model = vector_settings["vector_model"]
        vector_url = vector_settings["vector_url"]
        vector_dimension = vector_settings["vector_dimension"]
        vector_query_prefix = vector_settings.get("vector_query_prefix", "")
        vector_auth_type = vector_settings["vector_auth_type"]
        vector_has_api_key = vector_settings["vector_has_api_key"]
        vector_checked = "checked" if vector_enabled else ""
        vector_api_key_hint = (
            "Leave blank to keep the stored key."
            if vector_has_api_key
            else "Optional. Stored encrypted when provided."
        )
        vector_settings_html = f"""
    <div class="card">
      <h2>Search by meaning (vector search)</h2>
      <p class="text-muted" style="margin-bottom:12px">Enable semantic search using an embedding endpoint: Ollama, or any service with an OpenAI-compatible embeddings API. Save the settings to turn it on immediately when the backend is healthy. Disable to use plain text search only.</p>
      <form id="vector-settings-form" onsubmit="saveVectorSettings(event)">
        <label class="checkbox-label" style="margin-bottom:16px">
          <input type="checkbox" id="vector-search-enabled" {vector_checked}>
          Enable search by meaning
        </label>
        <div class="form-group">
          <label>Provider</label>
          <select id="vector-provider" style="width:200px" onchange="onProviderChange(this.value)">
            <option value="ollama" {"selected" if vector_provider == "ollama" else ""}>Ollama</option>
            <option value="openai" {"selected" if vector_provider == "openai" else ""}>OpenAI-compatible</option>
          </select>
          <p class="form-hint">Where embeddings come from: a local Ollama server, or any service with an OpenAI-style API.</p>
        </div>
        <div class="form-group">
          <label>Model</label>
          <input type="text" id="vector-model" value="{escape_html(vector_model)}" style="width:240px">
          <button type="button" class="btn btn-secondary btn-sm" onclick="loadVectorModels()">Load Models</button>
          <div style="margin-top:8px">
            <select id="vector-model-select" style="width:240px;display:none" onchange="if (this.value) document.getElementById('vector-model').value = this.value"></select>
          </div>
          <p class="form-hint" id="vector-model-hint">Type a model name, or click Load Models to pick one the endpoint reports.</p>
        </div>
        <div class="form-group">
          <label>URL</label>
          <input type="text" id="vector-url" value="{escape_html(vector_url)}" style="width:300px">
          <p class="form-hint" id="vector-url-hint">Base URL of the Ollama server, for example <code>http://localhost:11434</code>.</p>
        </div>
        <details class="access-details" id="vector-advanced" {"open" if vector_query_prefix else ""}><summary>Advanced: embedding dimension, query prefix</summary>
        <div class="form-group">
          <label>Embedding Dimension</label>
          <input type="number" id="vector-dimension" min="64" max="4096" value="{escape_html(vector_dimension)}" style="width:120px">
          <p class="form-hint">For reference only; Agent Core stores whatever length the model returns. <code>nomic-embed-text</code> uses 768.</p>
        </div>
        <div class="form-group">
          <label>Query Prefix</label>
          <input type="text" id="vector-query-prefix" value="{escape_html(vector_query_prefix)}" maxlength="100" style="width:200px" placeholder="none">
          <p class="form-hint">Added to search text only, never to stored memories, so changing it needs no re-embed. Use what the model card gives for queries, including any trailing space, for example <code>query: </code> for snowflake-arctic-embed. Only for models whose documents take no prefix; leave blank otherwise.</p>
        </div>
        </details>
        <div class="form-group">
          <label>Auth Type</label>
          <select id="vector-auth-type" style="width:200px">
            <option value="none" {"selected" if vector_auth_type == "none" else ""}>None</option>
            <option value="bearer" {"selected" if vector_auth_type == "bearer" else ""}>Bearer Token</option>
            <option value="api_key" {"selected" if vector_auth_type == "api_key" else ""}>API Key</option>
          </select>
          <p class="form-hint">How the endpoint expects the key below. Use None for a local server.</p>
        </div>
        <div class="form-group">
          <label>API Key</label>
          <input type="password" id="vector-api-key" value="" style="width:300px" autocomplete="off">
          <p class="form-hint">{escape_html(vector_api_key_hint)}</p>
        </div>
        <div class="form-actions">
          <button type="submit" class="btn">Save Vector Settings</button>
          <button type="button" class="btn btn-secondary" onclick="testVectorSettings()">Test Connection</button>
          <button type="button" class="btn btn-secondary" onclick="estimateVectorCost()">Estimate Cost</button>
        </div>
      </form>
      <div id="vector-settings-result" style="margin-top:12px"></div>
      <div id="vector-estimate-result" style="margin-top:12px"></div>
      <div style="margin-top:20px;border-top:1px solid var(--border, #ddd);padding-top:16px">
        <h3 style="margin:0 0 8px" class="sub-heading">Re-embed memory</h3>
        <p class="form-hint">Search only uses vectors made by the current model. After changing the model, re-embed so older memories are found by meaning again.</p>
        <details class="access-details"><summary>More</summary><p class="form-hint">Until then they are matched by text only. A run can be stopped and started again, and it skips memories already done. Each memory keeps one vector, so switching back later means re-embedding again.</p></details>
        <p id="vector-reembed-coverage" class="text-muted">Loading...</p>
        <div class="form-actions">
          <button type="button" class="btn btn-secondary" id="vector-reembed-start" onclick="startReembed()">Re-embed missing memories</button>
          <button type="button" class="btn btn-secondary" id="vector-reembed-cancel" onclick="cancelReembed()" style="display:none">Stop</button>
        </div>
        <div id="vector-reembed-result" style="margin-top:12px"></div>
      </div>
    </div>"""

    admin_modals = ""
    if is_admin:
        admin_modals = """
    <!-- Restore Modal -->
    <div class="modal-overlay" id="restore-modal" style="display:none">
      <div class="modal">
        <h2>Restore from Backup</h2>
        <div class="alert alert-danger">This replaces your current database and encryption key with the contents of the backup.</div>
        <form id="restore-form" onsubmit="doRestore(event)">
          <div class="form-group">
            <label>Backup file *</label>
            <input type="file" id="restore-file" accept=".zip,.enc,.zip.enc" required>
            <p class="form-hint">The backup you exported earlier (a .zip, or .enc if it was encrypted).</p>
          </div>
          <div class="form-group">
            <label>Backup Key</label>
            <input type="text" id="restore-backup-key" placeholder="Required for encrypted exports">
            <p class="form-hint">Encrypted backups need the one-time backup key shown at export time. Legacy unencrypted backups can still be restored without one.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="credential-key-rotate-modal" style="display:none">
      <div class="modal">
        <h2>Rotate Encryption Key</h2>
        <div class="alert alert-warning"><strong>Keeps working:</strong> every stored credential, every agent and every service. The old key stays in the key list, so nothing becomes unreadable.<br>
        <strong>Changes now:</strong> all stored credential values are re-encrypted with a new key. A backup made before this still restores, but take a new backup afterwards so it holds the new key.</div>
        <form id="credential-key-rotate-form" onsubmit="rotateCredentialKey(event)">
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('credential-key-rotate-modal')">Cancel</button>
            <button type="submit" class="btn btn-warning">Rotate Key</button>
          </div>
        </form>
      </div>
    </div>

    <div class="modal-overlay" id="broker-rotate-modal" style="display:none">
      <div class="modal">
        <h2>Rotate the local credential helper key</h2>
        <div class="alert alert-warning"><strong>Keeps working:</strong> agents, services, stored credentials, and any local program that reads the helper key from its file, which picks up the new key the next time it reads the file (the helper wrapper does, each time it starts).<br>
        <strong>Stops working:</strong> any program or script that was given the old helper key by hand, until you give it the new one. The new key is shown once, here, right after you rotate.</div>
        <div class="modal-footer">
          <button type="button" class="btn btn-secondary" onclick="closeModal('broker-rotate-modal')">Cancel</button>
          <button type="button" class="btn btn-warning" onclick="rotateBroker()">Rotate helper key</button>
        </div>
      </div>
    </div>

    <div class="modal-overlay" id="credential-key-restore-modal" style="display:none">
      <div class="modal">
        <h2>Restore Encryption Key</h2>
        <form id="credential-key-restore-form" onsubmit="restoreCredentialKey(event)">
          <div class="form-group">
            <label>Encryption key *</label>
            <input type="text" id="credential-key-restore-key" required>
            <p class="form-hint">The key must decrypt all current credential entries before it is accepted.</p>
          </div>
          <div class="modal-footer">
            <button type="button" class="btn btn-secondary" onclick="closeModal('credential-key-restore-modal')">Cancel</button>
            <button type="submit" class="btn btn-danger">Restore Key</button>
          </div>
        </form>
      </div>
    </div>"""

    js = """
    <script>
    async function saveUserSettings(e) {{
      e.preventDefault();
      const tz = document.getElementById('user-timezone').value;
      const j = await apiFetch('/api/dashboard/user-settings', {{ method: 'POST', body: JSON.stringify({{ timezone: tz }}) }});
      const res = document.getElementById('user-settings-result');
      if (j.ok) {{
        window.AC_USER_TZ = tz || (Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC');
        if (window.applyLocalTimes) window.applyLocalTimes(document);
        showToast('Preferences saved', 'success');
        res.innerHTML = '<div class="alert alert-success">Saved. Times now shown in ' + escapeHtml(window.AC_USER_TZ) + '.</div>';
      }} else {{
        res.innerHTML = '<div class="alert alert-danger">' + escapeHtml((j.error && j.error.message) || 'Save failed') + '</div>';
      }}
    }}
    window.saveUserSettings = saveUserSettings;

    async function loadOperatorTokens() {{
      const box = document.getElementById('op-token-list');
      if (!box) return;
      const j = await apiFetch('/api/auth/operator-tokens');
      const tokens = (j.ok && j.data.tokens) || [];
      if (!tokens.length) {{ box.innerHTML = '<p class="form-hint">No active operator tokens.</p>'; return; }}
      box.innerHTML = '<table class="table"><tr><th>Label</th><th>Expires</th><th>Last used</th><th></th></tr>' +
        tokens.map(t => '<tr><td>' + escapeHtml(t.label || '') + '</td><td>' + escapeHtml(t.expires_at) +
          '</td><td>' + escapeHtml(t.last_used_at || 'never') + '</td><td><button class="btn btn-sm" data-token-id="' +
          escapeHtml(t.id) + '" onclick="revokeOperatorToken(this.dataset.tokenId)">Revoke</button></td></tr>').join('') + '</table>';
    }}
    async function createOperatorToken() {{
      const body = {{ label: document.getElementById('op-token-label').value,
                     minutes: parseInt(document.getElementById('op-token-minutes').value, 10) }};
      const j = await apiFetch('/api/auth/operator-tokens', {{ method: 'POST', body: JSON.stringify(body) }});
      const res = document.getElementById('op-token-result');
      if (j.ok) {{
        res.innerHTML = '<div class="alert alert-success">Copy it now; it is not shown again. Expires ' + escapeHtml(j.data.expires_at) +
          '.<br><code style="user-select:all;word-break:break-all">' + escapeHtml(j.data.token) + '</code></div>';
        loadOperatorTokens();
      }} else {{
        res.innerHTML = '<div class="alert alert-danger">' + escapeHtml((j.error && j.error.message) || 'Could not create a token') + '</div>';
      }}
    }}
    async function revokeOperatorToken(id) {{
      const j = await apiFetch('/api/auth/operator-tokens/' + encodeURIComponent(id), {{ method: 'DELETE' }});
      if (j.ok) showToast('Token revoked', 'success');
      loadOperatorTokens();
    }}
    window.createOperatorToken = createOperatorToken;
    window.revokeOperatorToken = revokeOperatorToken;
    loadOperatorTokens();

    async function exportBackup() {{
      const made = await apiFetch('/api/backup/export', {{ method: 'POST' }});
      if (!made.ok) {{
        showToast(made.error?.message || 'Export failed', 'danger');
        return;
      }}
      const backupKey = made.data.backup_key;
      const r = await fetch(made.data.download_path);
      if (r.ok) {{
        const blob = await r.blob();
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url; a.download = made.data.filename || '{APP_SLUG}-backup.zip.enc'; a.click();
        URL.revokeObjectURL(url);
        const keyHtml = backupKey
          ? '<div class="alert alert-warning"><strong>Save this backup key now.</strong> It will not be shown again.<div class="api-key-display" style="margin-top:8px"><code>' + escapeHtml(backupKey) + '</code></div><button class="copy-btn" data-copy-value="' + escapeHtml(backupKey) + '">Copy</button></div>'
          : '<div class="alert alert-warning"><strong>Backup key missing from response.</strong></div>';
        document.getElementById('backup-result').innerHTML = keyHtml;
        showToast('Encrypted backup downloaded');
      }} else {{
        const j = await r.json();
        showToast(j.error?.message || 'Export failed', 'danger');
      }}
    }}
    async function runMaintenance() {{
      const r = await fetch('/api/backup/maintenance', {{ method: 'POST' }});
      const j = await r.json();
      if (j.ok) {{
        const plural = (n, one, many) => n + ' ' + (n === 1 ? one : many);
        const parts = [];
        const add = (n, one, many) => {{ if (n) parts.push(plural(n, one, many)); }};
        add(j.data.stale_activities_marked || 0, 'idle task marked', 'idle tasks marked');
        add(j.data.scratchpad_pruned || 0, 'temporary note deleted', 'temporary notes deleted');
        add(j.data.ttl_swept || 0, 'expired memory removed', 'expired memories removed');
        add(j.data.retracted_purged || 0, 'old retracted memory deleted', 'old retracted memories deleted');
        add(j.data.proposals_generated || 0, 'suggestion made', 'suggestions made');
        const outcome = parts.length ? parts.join(', ') : 'nothing to clean up';
        document.getElementById('backup-result').innerHTML =
          '<div class="alert alert-success">Maintenance complete: ' + escapeHtml(outcome) + '.</div>';
        const lastRunEl = document.getElementById('maintenance-last-run');
        if (lastRunEl) lastRunEl.textContent = 'Last run just now (manual): ' + outcome + '.';
        showToast('Maintenance complete');
      }} else {{ showToast(j.error?.message || 'Failed', 'danger'); }}
    }}
    async function runStartupChecks() {{
      const r = await fetch('/api/backup/startup-checks');
      const j = await r.json();
      if (j.ok) {{
        const checks = j.data.checks.map(c => '<div style="margin:4px 0"><span class="badge badge-' + (c.status==='OK'?'active':'stale') + '">' + c.status + '</span> ' + c.check + ': ' + c.message + '</div>').join('');
        document.getElementById('startup-checks').innerHTML = checks;
      }} else {{ showToast('Failed', 'danger'); }}
    }}
    async function rotateBroker() {{
      closeModal('broker-rotate-modal');
      const j = await apiFetch('/api/dashboard/broker/rotate', {{ method: 'POST' }});
      if (j.ok) {{
        document.getElementById('broker-result').innerHTML =
          '<div class="alert alert-danger"><strong>Save this credential now - it will not be shown again!</strong></div>' +
          '<div class="api-key-display"><code>' + escapeHtml(j.data.credential) + '</code></div>' +
          '<button class="copy-btn" data-copy-value="' + escapeHtml(j.data.credential) + '">Copy</button>';
      }} else {{ showToast(j.error.message || 'Failed', 'danger'); }}
    }}
    async function loadCredentialKeyStatus() {{
      const j = await apiFetch('/api/credentials/rotate/status');
      if (j.ok) {{
        const s = j.data.key_status;
        document.getElementById('credential-key-result').innerHTML =
          '<div class="alert alert-success">' + s.keyring_size + (s.keyring_size === 1 ? ' key' : ' keys') + ' in use' +
          '<details class="access-details" style="margin-top:6px"><summary>Details</summary>Mode: <code>' + s.mode + '</code> · Primary key ID: <code>' + s.primary_key_id + '</code></details></div>';
      }} else {{ showToast(j.error?.message || 'Status failed', 'danger'); }}
    }}
    async function rotateCredentialKey(e) {{
      e.preventDefault();
      const j = await apiFetch('/api/credentials/rotate', {{
        method: 'POST'
      }});
      if (j.ok) {{
        closeModal('credential-key-rotate-modal');
        document.getElementById('credential-key-rotate-form').reset();
        document.getElementById('credential-key-result').innerHTML =
          '<div class="alert alert-success">' + j.data.message + ' Re-encrypted: <code>' + j.data.re_encrypted_count + '</code>. Keys in use: <code>' + j.data.keyring_size + '</code>.</div>';
      }} else {{ showToast(j.error?.message || 'Rotation failed', 'danger'); }}
    }}
    async function restoreCredentialKey(e) {{
      e.preventDefault();
      const key = document.getElementById('credential-key-restore-key').value.trim();
      if (!key) {{ showToast('Enter key first', 'warning'); return; }}
      const j = await apiFetch('/api/credentials/restore-key', {{
        method: 'POST',
        body: JSON.stringify({{ key_base64: key }})
      }});
      if (j.ok) {{
        closeModal('credential-key-restore-modal');
        document.getElementById('credential-key-restore-form').reset();
        document.getElementById('credential-key-result').innerHTML = '<div class="alert alert-success">' + j.data.message + '</div>';
      }} else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function doRestore(e) {{
      e.preventDefault();
      const formData = new FormData();
      const file = document.getElementById('restore-file').files[0];
      const backupKey = document.getElementById('restore-backup-key').value.trim();
      if (!file) {{ showToast('Select a backup file', 'warning'); return; }}
      formData.append('backup', file);
      if (backupKey) formData.append('backup_key', backupKey);
      formData.append('mode', 'replace_all');
      const r = await fetch('/api/backup/restore', {{ method: 'POST', body: formData }});
      const j = await r.json();
      if (j.ok) {{ showToast('Restore complete'); closeModal('restore-modal'); }}
      else {{ showToast(j.error?.message || 'Restore failed', 'danger'); }}
    }}
    async function saveSystemSettings(e) {{
      e.preventDefault();
      const body = {{
        scratchpad_retention_days: document.getElementById('scratchpad-retention-days').value,
        retracted_retention_days: document.getElementById('retracted-retention-days').value,
        episodic_memory_ttl_days: document.getElementById('episodic-memory-ttl-days').value,
        memory_dedupe_similarity: document.getElementById('memory-dedupe-similarity').value,
        unconfirmed_inference_days: document.getElementById('unconfirmed-inference-days').value,
        unconfirmed_inference_min_importance: document.getElementById('unconfirmed-inference-min-importance').value,
        consolidation_scan_enabled: document.getElementById('consolidation-scan-enabled').checked ? 'true' : 'false',
        execution_log_retention_days: document.getElementById('execution-log-retention-days').value,
        webhook_log_retention_days: document.getElementById('webhook-log-retention-days').value,
      }};
      const j = await apiFetch('/api/dashboard/system-settings', {{
        method: 'POST',
        body: JSON.stringify(body)
      }});
      if (j.ok) {{
        const box = e.target.querySelector('.save-result');
        if (box) box.innerHTML = '<div class="alert alert-success">Saved.</div>';
      }} else {{
        showToast(j.error?.message || 'Failed to save settings', 'danger');
      }}
    }}
    async function saveVectorSettings(e) {{
      e.preventDefault();
      const provider = document.getElementById('vector-provider').value;
      const model = formVectorModel();
      const body = {{
        vector_search_enabled: document.getElementById('vector-search-enabled').checked ? 'true' : 'false',
        vector_provider: provider,
        vector_model: model,
        vector_url: document.getElementById('vector-url').value,
        vector_dimension: document.getElementById('vector-dimension').value,
        vector_query_prefix: document.getElementById('vector-query-prefix').value,
        vector_auth_type: document.getElementById('vector-auth-type').value,
      }};
      const apiKey = document.getElementById('vector-api-key').value;
      if (apiKey) body.vector_api_key = apiKey;
      const j = await apiFetch('/api/dashboard/vector-settings', {{
        method: 'POST',
        body: JSON.stringify(body)
      }});
      if (j.ok) {{
        document.getElementById('vector-api-key').value = '';
        document.getElementById('vector-model').value = model;
        document.getElementById('vector-settings-result').innerHTML = '<div class="alert alert-success">Vector settings saved.</div>';
        const changed = savedVectorModel !== null && model !== savedVectorModel;
        loadReembedStatus();
        if (changed && body.vector_search_enabled === 'true') {{
          document.getElementById('vector-settings-result').innerHTML = '<div class="alert alert-success">Vector settings saved. The model changed: existing memories are matched by text only until they are re-embedded.</div>';
          measureVectorCost(null, null);
        }}
      }} else {{
        showToast(j.error?.message || 'Failed to save vector settings', 'danger');
      }}
    }}
    let reembedTimer = null;
    let savedVectorModel = null;
    function fmtDuration(s) {{
      if (s < 90) return Math.round(s) + ' s';
      if (s < 5400) return Math.round(s / 60) + ' min';
      return (s / 3600).toFixed(1) + ' h';
    }}
    function formVectorModel() {{
      return document.getElementById('vector-model').value.trim();
    }}
    function formVectorAuth() {{
      // Only a key typed into the form is sent from here; the server pairs the
      // saved key with the saved URL itself and never sends it anywhere else.
      const auth = {{ provider: document.getElementById('vector-provider').value,
                      url: document.getElementById('vector-url').value.trim(),
                      auth_type: document.getElementById('vector-auth-type').value }};
      const key = document.getElementById('vector-api-key').value;
      if (key) auth.api_key = key;
      return auth;
    }}
    async function measureVectorCost(model, url) {{
      const out = document.getElementById('vector-estimate-result');
      out.innerHTML = '<div class="alert">Measuring ' + escapeHtml(model || 'the saved model') + ' on a sample as long as an average record (no memory content is sent). A model that is not loaded yet can take a minute...</div>';
      const body = url ? formVectorAuth() : {{}};
      if (model) body.model = model;
      if (url) body.url = url;
      const j = await apiFetch('/api/dashboard/vector-reembed/estimate', {{ method: 'POST', body: JSON.stringify(body) }});
      if (!j.ok) {{
        out.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Could not measure the model') + '</div>';
        return null;
      }}
      const d = j.data;
      const cls = {{ danger: 'alert-danger', warning: 'alert-warning', notice: '', ok: 'alert-success' }}[d.level] || '';
      out.innerHTML = '<div class="alert ' + cls + '"><strong>' + escapeHtml(d.model) + '</strong> (' + escapeHtml(String(d.dimension)) + ' dimensions): '
        + escapeHtml(String(d.seconds_per_record)) + ' s per record (sample of ' + escapeHtml(String(d.sample_chars)) + ' characters). '
        + 'Re-embedding the ' + escapeHtml(String(d.records_to_embed)) + ' memories without a vector for it would take about ' + escapeHtml(fmtDuration(d.backfill_seconds)) + '. '
        + 'Each memory write would wait about ' + escapeHtml(String(d.write_delay_seconds)) + ' s for embeddings (it embeds twice; the limit per call is ' + escapeHtml(String(d.embed_timeout_seconds)) + ' s). '
        + escapeHtml(d.message) + '</div>';
      return d;
    }}
    async function estimateVectorCost() {{
      await measureVectorCost(formVectorModel(), document.getElementById('vector-url').value.trim());
    }}
    function renderReembed(data) {{
      const c = data.coverage || {{}};
      if (c.model) savedVectorModel = c.model;
      const job = data.job;
      const cov = document.getElementById('vector-reembed-coverage');
      if (!cov) return;
      cov.textContent = 'Model ' + (c.model || '') + ': ' + (c.active_embedded || 0) + ' of ' + (c.active_total || 0)
        + ' active memories embedded' + (c.missing ? ' (' + c.missing + ' memories missing, including superseded and retracted).' : '.');
      const running = !!job && (job.status === 'running' || job.status === 'cancelling');
      document.getElementById('vector-reembed-start').disabled = running || !data.vector_search_enabled || !c.missing;
      document.getElementById('vector-reembed-cancel').style.display = running ? '' : 'none';
      const out = document.getElementById('vector-reembed-result');
      if (!job) {{
        out.innerHTML = data.vector_search_enabled ? '' : '<p class="form-hint" style="margin:0">Turn on search by meaning above to re-embed.</p>';
      }} else {{
        const counts = escapeHtml(String(job.embedded || 0)) + ' embedded, ' + escapeHtml(String(job.failed || 0)) + ' failed';
        const labels = {{ running: 'Running', cancelling: 'Stopping', completed: 'Last run finished', cancelled: 'Last run stopped', failed: 'Last run failed', interrupted: 'Last run was interrupted (server restart); start it again to finish' }};
        const cls = job.status === 'failed' ? 'alert-danger' : (job.status === 'completed' ? 'alert-success' : '');
        out.innerHTML = '<div class="alert ' + cls + '">' + escapeHtml(labels[job.status] || job.status) + ' for '
          + escapeHtml(job.model || '') + ': ' + counts + '.'
          + (job.error ? ' ' + escapeHtml(job.error) : '') + '</div>';
      }}
      clearTimeout(reembedTimer);
      if (running) reembedTimer = setTimeout(loadReembedStatus, 2000);
    }}
    async function loadReembedStatus() {{
      const j = await apiFetch('/api/dashboard/vector-reembed');
      if (j.ok) renderReembed(j.data);
    }}
    async function startReembed() {{
      const est = await measureVectorCost(null, null);
      if (!est) return;
      const warn = (est.level === 'danger' || est.level === 'warning') ? '\\n\\nWarning: ' + est.message : '';
      if (!confirm('Re-embed ' + est.records_to_embed + ' memories with ' + est.model + '? At ' + est.seconds_per_record + ' s per record this takes about ' + fmtDuration(est.backfill_seconds) + ' and runs in the background. Each record keeps one vector, so vectors from the previous model are replaced.' + warn)) return;
      const j = await apiFetch('/api/dashboard/vector-reembed', {{ method: 'POST' }});
      if (!j.ok) showToast(j.error?.message || 'Could not start the re-embed', 'danger');
      loadReembedStatus();
    }}
    async function cancelReembed() {{
      const j = await apiFetch('/api/dashboard/vector-reembed/cancel', {{ method: 'POST' }});
      if (!j.ok) showToast(j.error?.message || 'Could not stop the re-embed', 'danger');
      loadReembedStatus();
    }}
    document.addEventListener('DOMContentLoaded', () => {{ if (document.getElementById('vector-reembed-coverage')) loadReembedStatus(); }});
    async function testVectorSettings() {{
      const result = document.getElementById('vector-settings-result');
      result.innerHTML = '<div class="alert">Testing vector connection...</div>';
      const j = await apiFetch('/api/dashboard/vector-settings/test', {{ method: 'POST' }});
      if (j.ok) {{
        result.innerHTML = '<div class="alert alert-success">Vector connection succeeded for ' + escapeHtml(j.data.model || 'configured model') + '.</div>';
      }} else {{
        result.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Vector connection failed') + '</div>';
      }}
    }}
    function onProviderChange(provider) {{
      const hint = document.getElementById('vector-url-hint');
      if (provider === 'openai') {{
        hint.innerHTML = 'Base URL of the OpenAI-compatible API, usually ending in <code>/v1</code>, for example <code>https://api.openai.com/v1</code>. Most hosted services need an API key with Bearer auth.';
      }} else {{
        hint.innerHTML = 'Base URL of the Ollama server, for example <code>http://localhost:11434</code>.';
      }}
      const select = document.getElementById('vector-model-select');
      select.style.display = 'none';
      select.innerHTML = '';
    }}
    async function loadVectorModels() {{
      const select = document.getElementById('vector-model-select');
      const hint = document.getElementById('vector-model-hint');
      const auth = formVectorAuth();
      if (!auth.url) {{
        hint.textContent = 'Enter the URL first.';
        return;
      }}
      hint.textContent = 'Loading models...';
      const j = await apiFetch('/api/dashboard/vector-models', {{ method: 'POST', body: JSON.stringify(auth) }});
      const models = j.ok ? (j.data?.models || []) : [];
      if (!j.ok || models.length === 0) {{
        select.style.display = 'none';
        hint.textContent = j.ok ? 'The endpoint reported no models. Type the model name instead.'
          : (j.error?.message || 'Could not load models.') + ' You can still type the model name.';
        return;
      }}
      const current = formVectorModel();
      select.innerHTML = '<option value="">Choose a model...</option>'
        + models.map(name => `<option value="${{escapeHtml(name)}}">${{escapeHtml(name)}}</option>`).join('');
      if (models.includes(current)) select.value = current;
      select.style.display = '';
      hint.textContent = models.length + ' models reported. Not all of them are embedding models; pick one that is.';
    }}
    function initVectorProvider() {{
      const provider = document.getElementById('vector-provider');
      if (provider) onProviderChange(provider.value);
    }}
    document.addEventListener('DOMContentLoaded', initVectorProvider);
    window.saveVectorSettings = saveVectorSettings;
    window.testVectorSettings = testVectorSettings;
    window.startReembed = startReembed;
    window.estimateVectorCost = estimateVectorCost;
    window.cancelReembed = cancelReembed;
    window.onProviderChange = onProviderChange;
    window.loadVectorModels = loadVectorModels;
    </script>"""
    js = js.replace("{{", "{").replace("}}", "}")

    prune_html = ""
    if is_admin:
        prune_html = """
    <div class="card">
      <h2>Clear old activity</h2>
      <p class="form-hint" style="margin-top:0;margin-bottom:14px">Permanently deletes finished, cancelled and blocked tasks older than a date you choose. Tasks still in progress are left alone. This is separate from the limits above, which run on their own.</p>
      <div class="form-group">
        <label for="prune-activity-before">Delete tasks older than</label>
        <input type="date" id="prune-activity-before" style="max-width:200px">
        <p class="form-hint">Only finished, cancelled and blocked tasks older than this date are deleted.</p>
      </div>
      <button type="button" class="btn btn-danger" onclick="pruneActivity()">Delete</button>
      <div id="prune-activity-result" style="margin-top:12px"></div>
    </div>"""

    tabs = [("account", "Account", f"{account_html}{preferences_html}")]
    if is_admin:
        tabs += [
            ("memory", "Memory", f"{vector_settings_html}{memory_behavior_html}"),
            ("data", "Data and backup", f"{retention_html}{backup_html}{prune_html}"),
            ("security", "Security", f"{secrets_snapshot_html}{network_limits_html}{encryption_key_html}{broker_html}"),
        ]
    tab_bar = ""
    if len(tabs) > 1:
        tab_bar = '<div class="tabs" role="tablist">' + "".join(
            f'<button type="button" class="tab" role="tab" data-tab="{key}">{label}</button>' for key, label, _ in tabs
        ) + "</div>"
    tab_sections = "".join(
        f'<section class="tab-panel" id="tab-{key}" data-tab-panel="{key}">{html}</section>' for key, _, html in tabs
    )

    return render_page(
        "Settings",
        f"""
    <div class="page-header"><h1>Settings</h1></div>
    {tab_bar}
    {tab_sections}
    {admin_modals}
    """,
        "/settings",
        js + SETTINGS_TABS_JS + (PRUNE_JS if prune_html else ""),
        session=session,
    )


# ─── SETTINGS PASSWORD ──────────────────────────────────────────────────────────


@router.get("/settings/password")
def settings_password_page(request: Request, session: dict = Depends(require_auth)):
    js = """
    <script>
    function showPasswordStatus(message, type) {
      const box = document.getElementById('password-status');
      if (!box) return;
      box.className = 'alert alert-' + (type || 'success');
      box.textContent = message;
      box.style.display = 'block';
    }
    async function submitPassword(e) {
      e.preventDefault();
      const status = document.getElementById('password-status');
      if (status) {
        status.style.display = 'none';
        status.textContent = '';
      }
      const current = document.getElementById('current-password').value;
      const new_pw = document.getElementById('new-password').value;
      const confirm = document.getElementById('confirm-password').value;
      if (new_pw !== confirm) { showPasswordStatus('New passwords do not match', 'warning'); return; }
      if (new_pw.length < 8) { showPasswordStatus('Password must be at least 8 characters', 'warning'); return; }
      const j = await apiFetch('/api/auth/password', {
        method: 'POST',
        body: JSON.stringify({ current_password: current, new_password: new_pw })
      });
      if (j.ok) {
        e.target.reset();
        showPasswordStatus('Password updated. You can continue this session or use the logout button below to sign back in with the new password.', 'success');
      } else {
        showPasswordStatus(j.error?.message || 'Failed', 'danger');
      }
    }
    </script>"""
    return render_page(
        "Change Password",
        """
    <div class="page-header"><h1>Change Password</h1></div>
    <div class="card" style="max-width:500px">
      <h2>Update Your Password</h2>
      <div id="password-status" class="alert alert-success" style="display:none"></div>
      <form id="password-form" onsubmit="submitPassword(event)">
        <div class="form-group">
          <label>Current Password</label>
          <input type="password" id="current-password" autocomplete="current-password" required>
        </div>
        <div class="form-group">
          <label>New Password</label>
          <input type="password" id="new-password" minlength="8" autocomplete="new-password" required>
        </div>
        <div class="form-group">
          <label>Confirm New Password</label>
          <input type="password" id="confirm-password" minlength="8" autocomplete="new-password" required>
        </div>
        <div class="modal-footer" style="justify-content:flex-start;gap:8px;padding-left:0;padding-right:0">
          <button type="submit" class="btn">Update Password</button>
          <a class="btn btn-secondary" href="/logout">Log Out</a>
        </div>
      </form>
    </div>
    """,
        "/settings",
        js,
        session=session,
    )


# ─── SETTINGS OTP ───────────────────────────────────────────────────────────────


@router.get("/settings/otp")
def settings_otp_page(request: Request, session: dict = Depends(require_auth)):
    from app.services.auth_service import is_otp_enrolled

    enrolled = is_otp_enrolled(session["user_id"])

    if enrolled:
        body_html = """
        <div class="alert alert-success">Two-step sign-in is on for your account.</div>
        <p class="text-muted">To reset it you need your current password and a code from your authenticator app. Turn it off to remove it completely; you can set it up again later.</p>
        <form id="otp-reset-form" onsubmit="submitOtpReset(event)">
          <div class="form-group">
            <label for="otp-current-password">Current Password</label>
            <input type="password" id="otp-current-password" autocomplete="current-password" required>
          </div>
          <div class="form-group">
            <label for="otp-current-code">Current code</label>
            <input type="text" id="otp-current-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" required style="width:160px">
          </div>
          <button type="submit" class="btn btn-danger">Reset two-step sign-in</button>
        </form>
        <form id="otp-disable-form" onsubmit="submitOtpDisable(event)" style="margin-top:16px">
          <div class="form-group">
            <label for="otp-disable-password">Current Password</label>
            <input type="password" id="otp-disable-password" autocomplete="current-password" required>
          </div>
          <div class="form-group">
            <label for="otp-disable-code">Current code</label>
            <input type="text" id="otp-disable-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" required style="width:160px">
          </div>
          <button type="submit" class="btn btn-secondary">Turn off two-step sign-in</button>
        </form>
        <div id="otp-reset-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function submitOtpReset(e) {
          e.preventDefault();
          if (!confirm('Reset two-step sign-in? Your existing authenticator setup will be replaced.')) return;
          await startOtpEnrollment('otp-reset-result');
        }
        async function submitOtpDisable(e) {
          e.preventDefault();
          if (!confirm('Turn off two-step sign-in on this account?')) return;
          const password = document.getElementById('otp-disable-password')?.value || '';
          const otp = document.getElementById('otp-disable-code')?.value || '';
          if (!password || !otp) { showToast('Enter your current password and the code from your authenticator app', 'warning'); return; }
          const j = await apiFetch('/api/auth/otp/disable', {
            method: 'POST',
            body: JSON.stringify({ current_password: password, otp_code: otp })
          });
          if (j.ok) {
            document.getElementById('otp-disable-form').reset();
            document.getElementById('otp-reset-result').innerHTML = '<div class="alert alert-success"><strong>Two-step sign-in is off.</strong></div>';
          } else {
            showToast(j.error?.message || 'Failed', 'danger');
          }
        }
        </script>"""
    else:
        body_html = """
        <div class="alert alert-warning">Two-step sign-in is not set up.</div>
        <p class="text-muted">Two-step sign-in adds a second check when you sign in. Enter your current password, then scan the QR code with an authenticator app.</p>
        <div class="form-group">
          <label for="otp-current-password">Current Password</label>
          <input type="password" id="otp-current-password" autocomplete="current-password" required>
        </div>
        <button class="btn" onclick="enrollOtp()">Set up two-step sign-in</button>
        <div id="otp-enroll-result" style="margin-top:16px"></div>"""
        js = """
        <script>
        async function enrollOtp() {
          await startOtpEnrollment('otp-enroll-result');
        }
        </script>"""

    setup_js = """
        <script>
        async function startOtpEnrollment(targetId) {
          const target = document.getElementById(targetId);
          const password = document.getElementById('otp-current-password')?.value || '';
          const currentOtp = document.getElementById('otp-current-code')?.value || '';
          if (!password) { showToast('Enter your current password', 'warning'); return; }
        target.innerHTML = '<div class="alert alert-warning">Preparing two-step sign-in...</div>';
          const body = { current_password: password };
          if (currentOtp) body.otp_code = currentOtp;
          const j = await apiFetch('/api/auth/otp/enroll', {
            method: 'POST',
            body: JSON.stringify(body)
          });
          if (j.ok) {
            const qrHtml = j.data.qr_svg
              ? '<img class="otp-qr" src="' + j.data.qr_svg + '" alt="Two-step sign-in QR code">'
              : '<div class="alert alert-warning">QR code could not be generated. Use the manual secret instead.</div>';
            target.innerHTML =
              '<div class="alert alert-success"><strong>Scan this QR code in your authenticator app.</strong></div>' +
              '<div class="otp-enroll-layout">' +
                qrHtml +
                '<div>' +
                  '<p class="text-muted">Or enter this secret manually:</p>' +
                  '<div class="mono" style="word-break:break-all;margin:12px 0">' + escapeHtml(j.data.secret) + '</div>' +
                  '<label for="otp-confirm-code">Enter the 6-digit code from your app</label>' +
                  '<input type="text" id="otp-confirm-code" maxlength="6" pattern="[0-9]*" inputmode="numeric" style="width:160px">' +
                  '<div style="margin-top:10px"><button class="btn" data-otp-confirm="' + escapeHtml(targetId) + '">Verify and turn on</button></div>' +
                '</div>' +
              '</div>';
          } else {
            target.innerHTML = '<div class="alert alert-danger">' + escapeHtml(j.error?.message || 'Failed to prepare two-step sign-in') + '</div>';
            showToast(j.error?.message || 'Failed', 'danger');
          }
        }
        // The button carrying this id is rendered by this page, so its
        // listener belongs here: the main settings script is not loaded on
        // /settings/otp.
        document.addEventListener('click', function(ev) {
          const otp = ev.target.closest('[data-otp-confirm]');
          if (!otp) return;
          ev.preventDefault();
          confirmOtpEnrollment(otp.dataset.otpConfirm);
        });

        async function confirmOtpEnrollment(targetId) {
          const code = document.getElementById('otp-confirm-code')?.value || '';
          if (!code) { showToast('Enter the authenticator code', 'warning'); return; }
          const j = await apiFetch('/api/auth/otp/confirm', {
            method: 'POST',
            body: JSON.stringify({ otp_code: code })
          });
          if (j.ok) {
            document.getElementById(targetId).innerHTML =
              '<div class="alert alert-success"><strong>Two-step sign-in is on.</strong></div>';
          } else {
            showToast(j.error?.message || 'Invalid code', 'danger');
          }
        }
        </script>
    """
    js = setup_js + js

    return render_page(
        "Manage two-step sign-in",
        f"""
    <div class="page-header"><h1>Two-Factor Authentication</h1></div>
    <div class="card" style="max-width:600px">
      {body_html}
    </div>
    """,
        "/settings",
        js,
        session=session,
    )
